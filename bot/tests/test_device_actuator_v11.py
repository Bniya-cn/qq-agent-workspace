"""Mac 动作的权限、幂等、配额和 MuMu 验证边界；不触发真实设备副作用。"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nonebot
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
nonebot.init()
from src.plugins.agent_core.device_actuator import (
    MacNativeActuator, MacActionCoordinator, MuMuSimulatorVerifier,
    ActuatorExecutionResult, _communicate_process, _escape_applescript_str,
)
from src.plugins.agent_core import llm_service

OWNER = '1785381042'


class TestDeviceActuatorAndMuMuVerification(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mac = MacNativeActuator(Path(self.tmp.name)/'state.json')
        self.mac.cooldown_sec = 0
        self.coordinator = MacActionCoordinator(self.mac)

    async def test_owner_and_switch_apply_before_execution(self):
        with self.assertRaises(PermissionError):
            self.coordinator.prepare('999999999', {'action':'notify'})
        self.mac.enabled = False
        with self.assertRaises(PermissionError):
            self.coordinator.prepare(OWNER, {'action':'music','command':'play'})
        result = await self.mac.control_mac_music('play', uid=OWNER)
        self.assertFalse(result.ok)

    async def test_ai_only_prepares_by_default(self):
        with patch.object(llm_service, 'chat', AsyncMock(return_value='{"action":"notify","title":"休息一下","message":"辛苦了"}')), patch.object(self.coordinator, '_execute', AsyncMock()) as execute:
            job = await self.coordinator.plan_message(OWNER, '提醒我休息一下')
        self.assertEqual(job['status'], 'prepared')
        execute.assert_not_awaited()

    async def test_prepare_only_and_persisted_message_deduplication(self):
        self.coordinator.data['require_confirmation'] = False
        with patch.object(llm_service, 'chat', AsyncMock(return_value='{"action":"notify","title":"休息"}')) as chat, patch.object(self.coordinator, '_execute', AsyncMock()) as execute:
            job = await self.coordinator.plan_message(OWNER, '提醒我休息', prepare_only=True, event_key='private:1785381042:abc')
            self.assertEqual(job['status'], 'prepared')
            reloaded = MacActionCoordinator(MacNativeActuator(self.mac.state_path))
            again = await reloaded.plan_message(OWNER, '提醒我休息', event_key='private:1785381042:abc')
            self.assertEqual(again['id'], job['id'])
            self.assertEqual(chat.await_count, 1)
            execute.assert_not_awaited()

    async def test_confirmation_is_bound_to_owner_and_idempotent(self):
        job = self.coordinator.prepare(OWNER, {'action':'notify'})
        with self.assertRaises(PermissionError):
            await self.coordinator.confirm('999999999', job['id'])
        with patch.object(self.coordinator, '_execute', AsyncMock(return_value=ActuatorExecutionResult(True, 'notify', 'ok'))) as execute:
            results = await asyncio.gather(*[self.coordinator.confirm(OWNER, job['id']) for _ in range(2)])
        self.assertEqual(execute.await_count, 1)
        self.assertTrue(all(x['status'] == 'succeeded' for x in results))

    async def test_daily_quota_is_reserved_before_side_effect(self):
        self.mac.daily_max_actions = 1
        jobs = [self.coordinator.prepare(OWNER, {'action':'notify'}) for _ in range(2)]
        with patch.object(self.coordinator, '_execute', AsyncMock(return_value=ActuatorExecutionResult(True, 'notify', 'ok'))) as execute:
            results = await asyncio.gather(*[self.coordinator.confirm(OWNER, job['id']) for job in jobs], return_exceptions=True)
        self.assertEqual(execute.await_count, 1)
        self.assertTrue(any(isinstance(x, ValueError) for x in results))
        reloaded = MacActionCoordinator(MacNativeActuator(self.mac.state_path))
        self.assertEqual(reloaded.data['quota_count'], 1)

    async def test_cancel_and_uncertain_never_retry(self):
        job = self.coordinator.prepare(OWNER, {'action':'notify'})
        await self.coordinator.confirm(OWNER, job['id'], cancel=True)
        with patch.object(self.coordinator, '_execute', AsyncMock()) as execute:
            await self.coordinator.confirm(OWNER, job['id'])
        execute.assert_not_awaited()
        job = self.coordinator.prepare(OWNER, {'action':'notify'})
        with patch.object(self.coordinator, '_execute', AsyncMock(side_effect=TimeoutError())) as execute:
            await self.coordinator.confirm(OWNER, job['id'])
            await self.coordinator.confirm(OWNER, job['id'])
        self.assertEqual(job['status'], 'uncertain')
        self.assertEqual(execute.await_count, 1)

    async def test_subprocess_timeout_kills_and_reaps(self):
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time;time.sleep(30)', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        with self.assertRaises(TimeoutError):
            await _communicate_process(proc, .03)
        self.assertIsNotNone(proc.returncode)

    async def test_shortcut_allowlist_and_calendar_time_validation(self):
        with self.assertRaises(ValueError):
            self.coordinator.prepare(OWNER, {'action':'shortcut','shortcut_name':'任意脚本'})
        with self.assertRaises(ValueError):
            self.coordinator.prepare(OWNER, {'action':'calendar','start_at':'2026-10-26T10:00:00+08:00','end_at':'2026-10-26T09:00:00+08:00'})
        self.assertIn('\\"', _escape_applescript_str('" & do shell script "test'))

    async def test_mumu_disconnection_does_not_fabricate_live_screen(self):
        verifier = MuMuSimulatorVerifier()
        with patch.object(verifier, 'probe_mumu_connection', AsyncMock(return_value={'connected':False})):
            with self.assertRaises(RuntimeError):
                await verifier.capture_mumu_screen_or_synthetic()
        with self.assertRaises(ValueError):
            await verifier.verify_gui_step(dry_run=False)
        with self.assertRaises(ValueError):
            await verifier.probe_mumu_connection('8.8.8.8:5555')

    async def test_synthetic_test_is_labelled_and_coordinates_checked(self):
        verifier = MuMuSimulatorVerifier()
        with patch('src.plugins.agent_core.device_actuator.MUMU_SANDBOX_DIR', Path(self.tmp.name)):
            result = await verifier.verify_gui_step(force_synthetic=True, vision_planner=lambda *args:{'action':'tap','x':20,'y':30})
            self.assertEqual(result['verification_level'], 'synthetic')
            self.assertFalse(result['adb_executed'])
            normalized = await verifier.verify_gui_step(force_synthetic=True, vision_planner=lambda *args:{'action':'tap','x':1000,'y':500,'coordinate_system':'normalized_1000'})
            self.assertEqual(normalized['planned_action']['x'], 719)
            self.assertEqual(normalized['planned_action']['y'], 640)
            with self.assertRaises(ValueError):
                await verifier.verify_gui_step(force_synthetic=True, vision_planner=lambda *args:{'action':'tap','x':999999,'y':30})

    async def test_api_rejects_remote_clients_and_cross_site_writes(self):
        from src.plugins.agent_core import device_actuator
        with patch.object(device_actuator, 'mac_actions', self.coordinator):
            transport = httpx.ASGITransport(app=nonebot.get_app(), client=('192.168.1.2', 1234))
            async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
                response = await client.get('/api/actuator/status')
                self.assertEqual(response.status_code, 403)
            transport = httpx.ASGITransport(app=nonebot.get_app(), client=('127.0.0.1', 1234))
            async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
                response = await client.post('/api/actuator/config', json={'enabled':False}, headers={'Origin':'https://malicious.example'})
                self.assertEqual(response.status_code, 403)
                self.assertTrue(self.mac.enabled)
                response = await client.get('/api/actuator/status')
                self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
