"""隔离网络和 QQ，验证公开上下文、真实配置、发布生命周期与静态资源。"""
from __future__ import annotations
import asyncio
import base64
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx
import nonebot
from fastapi import FastAPI
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
nonebot.init()
from src.plugins import agent_core as core
from src.plugins.agent_core import config_manager as config, conversation_service as conv
from src.plugins.agent_core import local_rag_service as rag, llm_service as llm, controller_api as api, web_panel
from src.plugins.agent_core import history_recorder as history
from src.plugins.agent_core.modes import small_talk


class PersonaPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def session(self, uid="1785381042", turns=None):
        return conv.Session(uid, "test", turns or [], None)

    def test_owner_trust_only_in_authenticated_private_session(self):
        with conv.use_session(self.session()):
            self.assertIn("长期信赖", small_talk.amiya_context_directive())
        with conv.use_session(self.session(), group_id="10001"):
            self.assertNotIn("长期信赖", small_talk.amiya_context_directive())
        with conv.use_session(self.session("99999")):
            self.assertNotIn("长期信赖", small_talk.build_small_talk_system_prompt("amiya"))

    def test_amiya_pure_persona_has_no_meme_examples(self):
        for text in (small_talk.build_small_talk_system_prompt("amiya"), llm.build_operator_system("amiya")):
            self.assertIn("罗德岛", text)
            self.assertIn("领袖", text)
            for bad in ("好家伙", "笑死", "算盘打得", "彻底抛弃"):
                self.assertNotIn(bad, text)

    def test_group_history_excludes_private_and_legacy(self):
        turns = [{"user": "私密经历", "assistant": "私密回答", "visibility": "private"}, {"user": "旧来源未知", "assistant": "旧回答"}, {"user": "公开问题", "assistant": "公开回答", "visibility": "group"}]
        with conv.use_session(self.session(turns=turns), group_id="10001"):
            text = json.dumps(conv.messages("系统", "新问题"), ensure_ascii=False) + conv.app_prompt("新问题")
            self.assertIn("公开问题", text)
            self.assertNotIn("私密经历", text)
            self.assertNotIn("旧来源未知", text)
        with conv.use_session(self.session(turns=turns)):
            self.assertIn("私密经历", json.dumps(conv.messages("系统", "新问题"), ensure_ascii=False))

    def test_private_active_state_is_not_injected_publicly(self):
        session = self.session()
        with conv.use_session(session):
            conv.set_active_state({"has_active_state": True, "state_description": "私聊状态"})
        with conv.use_session(session, group_id="10001"):
            self.assertIsNone(conv.get_active_state())
            self.assertNotIn("私聊状态", conv.app_prompt("新问题"))

    def test_new_turn_keeps_qq_key_and_records_visibility(self):
        with tempfile.TemporaryDirectory() as folder:
            store = conv.ConversationStore(Path(folder)/"history.sqlite3")
            session = store.begin("1785381042")
            with conv.use_session(session): store.remember(session, "私聊问题", "回答")
            with conv.use_session(session, group_id="10001"): store.remember(session, "公开问题", "回答")
            loaded = store.begin("1785381042")
            self.assertEqual(loaded.session_id, session.session_id)
            self.assertEqual([t['visibility'] for t in loaded.history], ["private", "group"])

    async def test_bailian_payload_includes_persona_and_excludes_private_history(self):
        captured = []
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"output": {"text": "博士，我在听。"}})
        client = httpx.AsyncClient
        session = self.session(turns=[{"user": "不应公开的私聊", "assistant": "回答", "visibility": "private"}])
        with conv.use_session(session, group_id="10001"), patch.object(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs)):
            result = await llm._call_bailian_app("test-app", "test-key", "我是1785381042", style="amiya", max_tokens=60)
        self.assertEqual(result, "博士，我在听。")
        self.assertIn("温柔", captured[0]['input']['prompt'])
        self.assertNotIn("不应公开的私聊", captured[0]['input']['prompt'])
        self.assertNotIn("长期信赖", captured[0]['input']['prompt'])


class DailyAndPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cfg = config.ConfigManager(self.root/'config.json')
        self.cfg.active_groups = ["10001", "10002"]
        self.kb = rag.LocalVectorKnowledgeBase(self.root/'kb.sqlite3')
        for item in (patch.object(rag,'state',self.cfg), patch.object(rag,'vector_kb',self.kb), patch.object(rag,'SCHEDULE_STATE_PATH',self.root/'schedule.json'), patch.object(rag,'QZONE_HISTORY_PATH',self.root/'posts.json'), patch.object(rag,'_DAILY_JOBS',{}), patch.object(rag,'_DAILY_TASKS',{}), patch.object(rag,'_DAILY_SCHEDULER_TASK',None), patch.object(rag,'_WARM_HTTP_CLIENT',None), patch.object(history,'progress_tracker',history.TaskProgressTracker())):
            item.start(); self.addCleanup(item.stop)
        self.app = FastAPI(); rag.register_routes(self.app)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test')

    async def asyncTearDown(self):
        await rag.shutdown()
        await self.client.aclose()

    async def test_status_is_read_only_and_does_not_start_scheduler(self):
        with patch.object(rag,'ensure_daily_scheduler_started') as start:
            result = await self.client.get('/api/rag/status')
        self.assertEqual(result.status_code,200)
        start.assert_not_called()
        self.assertFalse(self.kb.db_path.exists())
        self.assertFalse(rag.SCHEDULE_STATE_PATH.exists())
        self.assertFalse(result.json()['summary']['daily_9am_enabled'])

    async def test_registration_is_idempotent(self):
        before = list(self.app.routes); rag.register_routes(self.app)
        self.assertEqual(before,self.app.routes)

    async def test_config_partial_update_boolean_and_time_validation(self):
        result = await self.client.post('/api/rag/config',json={'fast_reply_mode':'false'})
        self.assertEqual(result.status_code,200)
        self.assertFalse(self.cfg.fast_reply_mode)
        self.assertTrue(self.cfg.chat_1v1_enabled)
        self.assertFalse(self.cfg.daily_schedule_armed)
        result = await self.client.post('/api/rag/config',json={'daily_schedule_time':'25:90'})
        self.assertEqual(result.status_code,422)
        self.assertEqual(self.cfg.daily_schedule_time,'09:00')
        await self.client.post('/api/rag/config',json={'daily_9am_enabled':True})
        self.assertTrue(self.cfg.daily_schedule_armed)

    async def test_accept_poke_setting_persists_and_is_exposed_to_panel(self):
        result = await self.client.post('/api/rag/config',json={'accept_poke':False})
        self.assertEqual(result.status_code,200)
        self.assertFalse(self.cfg.accept_poke)
        status = await self.client.get('/api/rag/status')
        self.assertFalse(status.json()['summary']['accept_poke'])
        restored = config.ConfigManager(self.root/'config.json')
        self.assertFalse(restored.accept_poke)

        html = (web_panel.WEB_ROOT/'index.html').read_text(encoding='utf-8')
        script = (web_panel.WEB_ROOT/'panel.js').read_text(encoding='utf-8')
        self.assertIn('id="accept-poke-toggle"',html)
        self.assertIn("accept_poke: 'accept-poke-toggle'",script)

    async def test_daily_schedule_time_survives_backend_config_reload(self):
        result = await self.client.post('/api/rag/config',json={'daily_9am_enabled':True,'daily_schedule_time':'08:58'})
        self.assertEqual(result.status_code,200)
        restored = config.ConfigManager(self.root/'config.json')
        self.assertTrue(restored.daily_schedule_armed)
        self.assertTrue(restored.daily_9am_enabled)
        self.assertEqual(restored.daily_schedule_time,'08:58')

    async def test_unsupported_parallel_flag_is_not_reported_as_capability(self):
        result = await self.client.post('/api/rag/config',json={'parallel_sync_async_enabled':True})
        self.assertFalse(result.json()['summary']['parallel_sync_async_enabled'])
        self.assertFalse(self.cfg.parallel_sync_async_enabled)

    def test_scheduler_respects_armed_time_and_existing_attempt(self):
        now = datetime.datetime(2026,10,20,9,0,tzinfo=rag._DAILY_TIMEZONE)
        self.cfg.daily_schedule_time = '09:00'
        with patch.object(rag,'start_daily_action') as start:
            self.cfg.daily_9am_enabled = True
            rag._scheduler_tick(now); start.assert_not_called()
            self.cfg.daily_schedule_armed = True
            rag._scheduler_tick(now.replace(hour=8)); start.assert_not_called()
            rag._save_schedule_state({'qzone_job':{'date':'2026-10-20','status':'unknown'},'morning_job':{'date':'2026-10-20','status':'error'}})
            rag._scheduler_tick(now); start.assert_not_called()
            rag._save_schedule_state({})
            rag._scheduler_tick(now + datetime.timedelta(minutes=4))
            self.assertEqual(start.call_count,2)
            start.reset_mock()
            rag._scheduler_tick(now + datetime.timedelta(minutes=6))
            start.assert_not_called()

    async def test_daily_task_has_deduplication_and_terminal_receipt(self):
        release = asyncio.Event()
        async def publish(*args,**kwargs):
            await release.wait()
            return {'code':0,'status':'published','status_label':'已确认发布'}
        with patch.object(rag,'publish_qzone_post',AsyncMock(side_effect=publish)) as send:
            first = rag.start_daily_action('qzone',request_id='one',with_image=False)
            duplicate = rag.start_daily_action('qzone',request_id='one',with_image=False)
            self.assertEqual(first['task_id'],duplicate['task_id'])
            release.set(); await rag._DAILY_TASKS['qzone']
            repeated = rag.start_daily_action('qzone',request_id='one',with_image=False)
            self.assertEqual(repeated['status'],'published')
            self.assertEqual(send.await_count,1)
            self.assertEqual(history.progress_tracker.tasks[first['task_id']]['status'],'done')

    async def test_interrupted_job_is_unknown_and_not_resent(self):
        date = rag._daily_now().date().isoformat()
        job = {'request_id':'one','date':date,'status':'running','task_id':'old'}
        rag._save_daily_job('qzone',job)
        result = rag.start_daily_action('qzone',request_id='one')
        self.assertEqual(result['status'],'unknown')
        self.assertFalse(rag._DAILY_TASKS)
        self.assertEqual(rag.get_fast_engine_summary()['daily_jobs']['qzone']['status'],'unknown')

    async def test_cancellation_persists_unknown_and_releases_worker(self):
        started = asyncio.Event()
        async def publish(*args,**kwargs):
            started.set(); await asyncio.Event().wait()
        with patch.object(rag,'publish_qzone_post',AsyncMock(side_effect=publish)):
            rag.start_daily_action('qzone',request_id='cancel')
            await started.wait()
            task = rag._DAILY_TASKS['qzone']; task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(rag._load_schedule_state()['qzone_job']['status'],'unknown')

    async def test_acknowledging_unknown_result_does_not_send(self):
        job={'request_id':'unknown','task_id':'task-unknown','date':rag._daily_now().date().isoformat(),'status':'unknown','code':1}
        rag._save_daily_job('qzone',job)
        with patch.object(rag,'publish_qzone_post',AsyncMock()) as send:
            wrong=await self.client.post('/api/daily/qzone/acknowledge',json={'task_id':'wrong'})
            self.assertEqual(wrong.status_code,409)
            result=await self.client.post('/api/daily/qzone/acknowledge',json={'task_id':'task-unknown'})
        self.assertEqual(result.status_code,200)
        self.assertTrue(result.json()['job']['can_start'])
        send.assert_not_awaited()
        self.assertEqual(rag._load_schedule_state()['qzone_job']['status'],'acknowledged')

    async def test_no_recipient_is_not_success_and_skips_generation(self):
        with patch('nonebot.get_bots',return_value={}), patch.object(rag,'generate_morning_tarot_quote',AsyncMock()) as generate:
            result = await rag.broadcast_morning_tarot_to_groups(with_image=False)
        self.assertEqual(result['code'],1); generate.assert_not_awaited()
        self.assertFalse(rag._load_schedule_state().get('last_morning_date'))

    async def test_group_timeout_is_unknown_without_retransmission(self):
        bot = SimpleNamespace(call_api=AsyncMock(side_effect=TimeoutError()))
        self.cfg.active_groups=['10001']
        with patch('nonebot.get_bots',return_value={'bot':bot}), patch.object(rag.asyncio,'sleep',AsyncMock()):
            result = await rag.broadcast_morning_tarot_to_groups('早安',with_image=False)
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(result['unknown_groups'],['10001'])
        self.assertEqual(bot.call_api.await_count,1)
        self.assertFalse(rag._load_schedule_state().get('last_morning_date'))

    async def test_group_receipts_track_partial_success(self):
        bot = SimpleNamespace(call_api=AsyncMock(side_effect=[{'message_id':123},ActionFailed(retcode=1400, message='rejected')]))
        with patch('nonebot.get_bots',return_value={'bot':bot}), patch.object(rag.asyncio,'sleep',AsyncMock()):
            result = await rag.broadcast_morning_tarot_to_groups('早安',with_image=False)
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['sent_groups'],['10001'])
        self.assertEqual(result['failed_groups'],['10002'])
        self.assertEqual(result['code'],1)

    async def test_qzone_generated_without_connection_is_not_published(self):
        with patch('nonebot.get_bots',return_value={}):
            result = await rag.publish_qzone_post('今天一起认真工作',with_image=False)
        self.assertEqual(result['code'],1)
        self.assertFalse(result['published_online'])
        self.assertFalse(rag._load_schedule_state().get('last_qzone_date'))

    async def test_daily_image_uses_local_amiya_reference_and_fixed_model(self):
        reference = rag.BOT_ROOT / 'data' / 'refs' / '阿米娅.png'
        self.assertTrue(reference.is_file())
        raw = b'\x89PNG\r\n\x1a\n' + b'x' * 2048
        image_uri = 'base64://' + base64.b64encode(raw).decode('ascii')
        generator = AsyncMock(return_value=image_uri)
        with patch.dict('os.environ', {'DASHSCOPE_API_KEY': 'test-key'}), \
             patch.object(rag, '_get_warm_client') as warm_client, \
             patch('src.plugins.agent_core.llm_service.generate_image_with_qwen_or_wan', generator):
            result = await rag._try_generate_validated_image(
                '阿米娅在办公桌前认真工作',
                use_amiya_ref=True,
                timeout_sec=rag.DAILY_IMAGE_TIMEOUT_SEC,
                image_model=rag.DAILY_IMAGE_MODEL,
            )

        self.assertEqual(result, raw)
        warm_client.assert_not_called()
        kwargs = generator.await_args.kwargs
        self.assertEqual(kwargs['image_model'], 'qwen-image-3.0-pro')
        self.assertEqual(kwargs['generation_timeout_sec'], 240.0)
        self.assertTrue(kwargs['character_reference_only'])
        uri = kwargs['image_b64_list'][0]
        self.assertTrue(uri.startswith('data:image/png;base64,'))
        self.assertEqual(base64.b64decode(uri.split(',', 1)[1]), reference.read_bytes())

    async def test_daily_image_result_download_allows_slow_image_host(self):
        raw = b'\x89PNG\r\n\x1a\n' + b'x' * 2048
        client = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(status_code=200, content=raw)))
        with patch.dict('os.environ', {'DASHSCOPE_API_KEY': 'test-key'}), \
             patch.object(rag, '_get_warm_client', return_value=client), \
             patch('src.plugins.agent_core.llm_service.generate_image_with_qwen_or_wan', AsyncMock(return_value='https://images.example/amiya.png')):
            result = await rag._try_generate_validated_image(
                '阿米娅在办公室工作',
                image_model=rag.DAILY_IMAGE_MODEL,
                timeout_sec=rag.DAILY_IMAGE_TIMEOUT_SEC,
            )

        self.assertEqual(result, raw)
        self.assertEqual(client.get.await_args.kwargs['timeout'], 90.0)

    async def test_scheduled_tarot_uses_fixed_model_and_character_reference(self):
        self.cfg.active_groups = ['10001']
        bot = SimpleNamespace(call_api=AsyncMock(return_value={'message_id': 123}))
        fake_png = b'\x89PNG\r\n\x1a\n' + b'x' * 2048
        with patch('nonebot.get_bots', return_value={'bot': bot}), \
             patch.object(rag, 'generate_morning_tarot_quote', AsyncMock(return_value=('今日塔罗', '太阳·正位'))), \
             patch.object(rag, '_try_generate_validated_image', AsyncMock(return_value=fake_png)) as image, \
             patch.object(rag.asyncio, 'sleep', AsyncMock()):
            await rag.broadcast_morning_tarot_to_groups(with_image=True)

        self.assertTrue(image.await_args.kwargs['use_amiya_ref'])
        self.assertEqual(image.await_args.kwargs['image_model'], rag.DAILY_IMAGE_MODEL)
        self.assertEqual(image.await_args.kwargs['timeout_sec'], rag.DAILY_IMAGE_TIMEOUT_SEC)

    async def test_afternoon_qzone_generates_afternoon_copy_and_allows_longer_image_wait(self):
        class AfternoonDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 20, 15, 30, tzinfo=tz)

        bot = SimpleNamespace(self_id='1412773476', call_api=AsyncMock(return_value={'cookies': ''}))
        with patch.object(rag.datetime, 'datetime', AfternoonDateTime), \
             patch('nonebot.get_bots', return_value={'1412773476': bot}), \
             patch.object(rag, '_call_full_agent_with_preset', AsyncMock(return_value='午后阳光透过窗子，我正坐在电脑前认真工作呢☀️')) as agent, \
             patch.object(rag, '_try_generate_validated_image', AsyncMock(return_value=b'generated-image')) as image:
            await rag.publish_qzone_post('', with_image=True)

        self.assertIn('下午工作动态说说', agent.await_args.args[1])
        self.assertIn('午后阳光', image.await_args.args[0])
        self.assertEqual(image.await_args.kwargs['timeout_sec'], 240.0)
        self.assertEqual(image.await_args.kwargs['image_model'], rag.DAILY_IMAGE_MODEL)

    async def test_group_network_error_is_unknown(self):
        self.cfg.active_groups=['10001']
        bot=SimpleNamespace(call_api=AsyncMock(side_effect=NetworkError('connection lost')))
        with patch('nonebot.get_bots',return_value={'bot':bot}), patch.object(rag.asyncio,'sleep',AsyncMock()):
            result=await rag.broadcast_morning_tarot_to_groups('早安',with_image=False)
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(bot.call_api.await_count,1)

    def test_old_unknown_job_does_not_block_new_day_manual_action(self):
        saved={'qzone_job':{'date':'2000-01-01','status':'unknown'}}
        self.assertTrue(rag._daily_job_status('qzone',saved)['can_start'])
        saved['qzone_job']['date']=rag._daily_now().date().isoformat()
        self.assertFalse(rag._daily_job_status('qzone',saved)['can_start'])

    async def test_confirmed_qzone_receipt_survives_archive_failure(self):
        bot=SimpleNamespace(self_id='1412773476',call_api=AsyncMock(return_value={'cookies':'skey=test'}))
        client=SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200,json={'code':0,'tid':'post-1'})))
        with patch('nonebot.get_bots',return_value={'bot':bot}), patch.object(rag,'_get_warm_client',return_value=client), patch.object(rag,'_save_schedule_state',side_effect=OSError('disk full')):
            result=await rag.publish_qzone_post('早安',with_image=False)
        self.assertEqual(result['status'],'published')
        self.assertIn('persistence_warning',result)

    async def test_qzone_requires_explicit_post_id(self):
        bot = SimpleNamespace(self_id='1412773476',call_api=AsyncMock(return_value={'cookies':'skey=test'}))
        client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200,json={'code':0})))
        with patch('nonebot.get_bots',return_value={'bot':bot}), patch.object(rag,'_get_warm_client',return_value=client):
            with self.assertRaises(rag.DeliveryUncertain): await rag.publish_qzone_post('早安',with_image=False)
        self.assertEqual(client.post.await_count,1)
        self.assertEqual(bot.call_api.await_args.args[0],'get_cookies')

    async def test_qzone_explicit_receipt_is_published(self):
        bot = SimpleNamespace(self_id='1412773476',call_api=AsyncMock(return_value={'cookies':'skey=test'}))
        client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200,json={'code':0,'tid':'post-1'})))
        with patch('nonebot.get_bots',return_value={'bot':bot}), patch.object(rag,'_get_warm_client',return_value=client):
            result = await rag.publish_qzone_post('早安',with_image=False)
        self.assertEqual(result['status'],'published')
        self.assertEqual(rag._load_schedule_state()['last_qzone_date'],rag._daily_now().date().isoformat())
        self.assertFalse(result['has_image'])

    async def test_knowledge_migration_preserves_customized_rows(self):
        with self.kb._connect() as db:
            seed=rag._SEED_KNOWLEDGE[0]
            for content in (rag.LEGACY_AMIYA_CORE,'我的自定义内容'):
                db.execute("INSERT INTO kb_chunks(character,category,title,content,keywords,local_vec,cloud_vec,updated_at) VALUES ('amiya','persona_core',?,?,?,'[]','','')",(seed['title'],content,seed['keywords']))
        self.kb.ensure_initialized()
        contents=[item['content'] for item in self.kb._cache]
        self.assertIn('我的自定义内容',contents)
        self.assertIn(rag._SEED_KNOWLEDGE[0]['content'],contents)
        self.assertNotIn(rag.LEGACY_AMIYA_CORE,contents)

    async def test_meme_knowledge_only_participates_in_explicit_group_mimic(self):
        self.kb.ensure_initialized()
        self.cfg.small_talk_style='pure_persona'
        hits,_=await self.kb.search('TGA GTA6',top_k=20)
        self.assertFalse(any(h['category']=='gaming_meme' for h in hits))
        self.cfg.small_talk_style='group_mimic'
        hits,_=await self.kb.search('TGA GTA6',top_k=20)
        self.assertFalse(any(h['category']=='gaming_meme' for h in hits))
        self.cfg.small_talk_mode=True
        hits,_=await self.kb.search('TGA GTA6',top_k=20)
        self.assertTrue(any(h['category']=='gaming_meme' for h in hits))

    async def test_public_daily_generation_detaches_private_session(self):
        async def call(character,user,max_tokens):
            self.assertIsNone(conv.current())
            self.assertEqual(character,'amiya')
            return '早安博士'
        session=conv.Session('1785381042','test',[],None)
        with conv.use_session(session), patch.object(llm,'chat_operator',AsyncMock(side_effect=call)) as operator:
            self.assertEqual(await rag._call_full_agent_with_preset('amiya','早安'),'早安博士')
            self.assertIs(conv.current(),session)
        operator.assert_awaited_once()

    async def test_actual_fast_route_and_long_task_exclusion(self):
        self.cfg.active_character='amiya'; self.cfg.fast_reply_mode=True
        with patch.object(core,'state',self.cfg), patch.object(rag,'fast_chat_reply',AsyncMock(return_value='博士，我在听。')) as fast, patch.object(llm,'chat_operator',AsyncMock(return_value='完整正文')) as normal:
            result=await core._dispatch_reply_impl(uid='1785381042',group_id=None,user_text='今天累了')
            self.assertEqual(result,'博士，我在听。'); fast.assert_awaited_once()
            result=await core._dispatch_reply_impl(uid='1785381042',group_id=None,user_text='分析问题',is_long_task=True)
            self.assertEqual(result,'完整正文'); normal.assert_awaited_once()

    async def test_fast_voice_path_keeps_existing_voice_constraints(self):
        self.cfg.active_character='amiya'; self.cfg.fast_reply_mode=True
        with patch.object(core,'state',self.cfg), patch.object(rag,'fast_chat_reply',AsyncMock(return_value='博士，我在听。')) as fast:
            await core._dispatch_reply_impl(uid='1785381042',group_id=None,user_text='今天累了',voice_mode_only=True)
        self.assertIn('纯语音回复模式',fast.await_args.args[1])
        self.assertTrue(fast.await_args.kwargs['voice_mode_only'])

    async def test_fast_path_error_returns_to_existing_model(self):
        self.cfg.active_character='amiya'; self.cfg.fast_reply_mode=True
        with patch.object(core,'state',self.cfg), patch.object(rag,'fast_chat_reply',AsyncMock(side_effect=RuntimeError('local database failed'))), patch.object(llm,'chat_operator',AsyncMock(return_value='正常回答')) as normal:
            result=await core._dispatch_reply_impl(uid='1785381042',group_id=None,user_text='今天累了')
        self.assertEqual(result,'正常回答'); normal.assert_awaited_once()

    async def test_optional_kb_failure_does_not_fail_service_startup(self):
        with patch.object(self.kb,'ensure_initialized',side_effect=RuntimeError('damaged db')), patch.object(rag,'ensure_daily_scheduler_started') as start, patch.object(rag,'_RAG_INITIALIZATION_ERROR',''):
            await rag.startup()
            self.assertTrue(rag.get_fast_engine_summary()['initialization_error'])
        start.assert_called_once()

    async def test_panel_assets_reload_and_legacy_string_interface(self):
        web=self.root/'web'; web.mkdir(); (web/'index.html').write_text('<html>第一版</html>'); (web/'panel.js').write_text('const ok = true;')
        app=FastAPI(); api._register_routes(app,core.services)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            with patch.object(web_panel,'WEB_ROOT',web):
                self.assertIn('第一版',(await client.get('/panel')).text)
                self.assertIsInstance(web_panel.PANEL_HTML,str)
                (web/'index.html').write_text('<html>第二版</html>')
                self.assertIn('第二版',(await client.get('/panel')).text)
                asset=await client.get('/panel-assets/panel.js')
                self.assertEqual(asset.status_code,200); self.assertIn('no-store',asset.headers['cache-control'])
                self.assertIn('javascript',asset.headers['content-type'])
                self.assertEqual((await client.get('/panel-assets/config.json')).status_code,404)

if __name__=='__main__': unittest.main()
