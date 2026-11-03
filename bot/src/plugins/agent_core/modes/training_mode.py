"""训练模式与受控后台任务队列（Training Mode + Async Job Queue）。

满足要求 B & G：
1. 严格对话配对卫生：仅在存在真实引用关系（`reply_to_text` / `reply_to_message_id`）时构建 SFT 问答样本，绝不把时间相近但无关的两个人发言硬配成问答对！
2. 严禁自污染：过滤机器人自身（`sender_uid == bot_self_id` 或昵称含阿米娅/大肥鱼/奶蛙）的旧回复，绝不回灌进训练语料或短句池。
3. 受控后台任务队列：耗时训练、历史拉取与场景表达提炼进入后台队列，接口立即返回 `task_id` 与真实状态（`pending` -> `running` -> `done` | `error`）；训练失败如实标记 `status="error"` 与 `error_message`，绝不把失败记录为“训练完成”。
4. 非阻塞磁盘扫描：文件行数统计与语料读取通过 `asyncio.to_thread` 执行，不阻塞事件循环；提供明确的生命周期启动、取消与关停清理。
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Deque, Optional

from nonebot import logger

from ..config_manager import BOT_ROOT, state
from .. import log_extractor
from ..expression_learner import expression_store, is_persona_compatible_style

TRAINING_DIR = BOT_ROOT / "data" / "training"
GROUPS_DIR = BOT_ROOT / "data" / "groups"
AUTO_TRAIN_EVERY_N_MSGS = 35

_CQ_CLEAN_RE = re.compile(r"\[CQ:[^\]]+\]|\[(?:image|face|record|video|at|reply|forward|json):[^\]]+\]")
_NOISE_PATTERNS = (
    "撤回了一条消息",
    "加入了群聊",
    "邀请",
    "拍了拍",
    "分享卡片",
    "聊天记录预览",
    "处理请求超时",
    "稍后重试",
    "本鱼",
    "奶蛙",
)
_BOT_NAMES = ("阿米娅", "amiya", "大肥鱼", "奶蛙", "凯尔希", "达妮娅")

_msg_counter_since_train: dict[str, int] = {}
_training_locks: dict[str, asyncio.Lock] = {}
_training_stats: dict[str, dict[str, Any]] = {}
_recent_msg_by_id: dict[str, dict[str, dict[str, Any]]] = {}


@dataclasses.dataclass
class TrainingJobRecord:
    task_id: str
    group_id: str
    kind: str = "group_training"
    status: str = "pending"  # pending | running | done | error | cancelled
    created_at: float = dataclasses.field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    error_message: str = ""
    result: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class TrainingJobManager:
    """受控后台训练任务队列管理器（支持任务状态查询、错误如实记录与热重载优雅清理）。"""

    def __init__(self, max_concurrent: int = 1) -> None:
        self._semaphore = asyncio.Semaphore(max(1, int(max_concurrent)))
        self._jobs: collections.OrderedDict[str, TrainingJobRecord] = collections.OrderedDict()
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}
        self._max_history = 80

    def submit_job(
        self,
        group_id: str,
        coro_factory,
        *,
        kind: str = "group_training",
    ) -> TrainingJobRecord:
        gid = str(group_id or "").strip()
        # 若同群已有正在排队或运行的同类任务，直接复用返回该任务，防止重复触发
        for existing in reversed(self._jobs.values()):
            if existing.group_id == gid and existing.kind == kind and existing.status in {"pending", "running"}:
                return existing

        task_id = f"train_{gid}_{uuid.uuid4().hex[:8]}"
        rec = TrainingJobRecord(task_id=task_id, group_id=gid, kind=kind, status="pending")
        self._jobs[task_id] = rec
        while len(self._jobs) > self._max_history:
            self._jobs.popitem(last=False)

        async def _runner() -> None:
            async with self._semaphore:
                rec.status = "running"
                rec.started_at = time.time()
                try:
                    res = await coro_factory()
                    rec.finished_at = time.time()
                    if isinstance(res, dict):
                        rec.result = res
                        res_status = str(res.get("status") or "ok").lower()
                        if res_status in {"error", "failed"}:
                            rec.status = "error"
                            rec.error_message = str(res.get("message") or "训练任务执行失败")
                        elif res_status == "empty":
                            rec.status = "error"
                            rec.error_message = str(res.get("message") or "无可用训练语料")
                        else:
                            rec.status = "done"
                    else:
                        rec.status = "done"
                except asyncio.CancelledError:
                    rec.finished_at = time.time()
                    rec.status = "cancelled"
                    rec.error_message = "训练任务已取消"
                    raise
                except Exception as exc:
                    rec.finished_at = time.time()
                    rec.status = "error"
                    rec.error_message = f"{type(exc).__name__}: {exc}"
                    logger.warning(f"[训练任务失败] task_id={task_id} group={gid}: {exc}")
                finally:
                    self._active_tasks.pop(task_id, None)

        task = asyncio.create_task(_runner(), name=f"training_job_{task_id}")
        self._active_tasks[task_id] = task
        return rec

    def get_job(self, task_id: str) -> Optional[dict[str, Any]]:
        rec = self._jobs.get(str(task_id or "").strip())
        return rec.to_dict() if rec else None

    def list_jobs(self, group_id: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        items = list(reversed(self._jobs.values()))
        if group_id:
            gid = str(group_id).strip()
            items = [x for x in items if x.group_id == gid]
        return [x.to_dict() for x in items[:limit]]

    async def shutdown(self) -> None:
        """优雅取消并清理所有尚未结束的后台训练任务，避免热重载残留。"""
        tasks = list(self._active_tasks.values())
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.clear()


training_job_manager = TrainingJobManager(max_concurrent=1)


def is_training_group(group_id: Optional[str]) -> bool:
    """判断指定群是否开启了自动采集与训练。"""
    if not group_id or not state.training_mode:
        return False
    gid = str(group_id).strip()
    targets = {str(g).strip() for g in (state.training_groups or []) if str(g).strip()}
    if targets:
        return gid in targets
    allowed = {str(g).strip() for g in (state.active_groups or []) if str(g).strip()}
    return gid in allowed


def _clean_group_text(raw: str) -> str:
    s = _CQ_CLEAN_RE.sub("", raw or "").strip()
    s = re.sub(r"\s+", " ", s)
    if not s or len(s) > 300:
        return ""
    if s.startswith("/"):
        return ""
    if any(noise in s for noise in _NOISE_PATTERNS):
        return ""
    if not is_persona_compatible_style(s[:28]):
        return ""
    return s


def _append_short_reply_pool(group_id: str, text: str) -> None:
    """将群内 2~14 字且与阿米娅人格兼容的自然短句归入短句池。"""
    if not (2 <= len(text) <= 14):
        return
    if re.search(r"https?://|[0-9]{7,}", text):
        return
    if not is_persona_compatible_style(text):
        return
    pool_path = TRAINING_DIR / f"{group_id}_short_pool.json"
    try:
        TRAINING_DIR.mkdir(parents=True, exist_ok=True)
        pool: list[str] = []
        if pool_path.is_file():
            loaded = json.loads(pool_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                pool = [str(x) for x in loaded if isinstance(x, str)]
        if text not in pool:
            pool.append(text)
            pool = pool[-120:]
            pool_path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def record_group_message_for_training(
    group_id: str,
    sender_uid: str,
    sender_name: str,
    raw_text: str,
    *,
    reply_to_text: str = "",
    reply_to_message_id: Optional[str] = None,
    message_id: str = "",
    bot_self_id: str = "",
) -> bool:
    """实时记录群聊消息到训练语料库与场景表达缓冲池：
    - 严禁将机器人自己的回复回灌进语料库；
    - 仅当存在明确引用关系（reply_to_text 或 reply_to_message_id 命中非本人历史消息）时构建 SFT 问答对，绝不将相邻无关发言硬配成对！
    """
    gid = str(group_id or "").strip()
    uid = str(sender_uid or "").strip()
    if not is_training_group(gid) or not uid:
        return False
    if bot_self_id and uid == str(bot_self_id).strip():
        return False
    who = (sender_name or uid or "群友").strip()
    if any(b in who.lower() for b in _BOT_NAMES):
        return False

    clean_txt = _clean_group_text(raw_text)
    if not clean_txt:
        return False

    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    raw_log_path = TRAINING_DIR / f"{gid}_raw.log"
    dataset_path = TRAINING_DIR / f"{gid}_dataset.jsonl"

    now_ts = int(time.time())
    is_luis = "路易斯" in who.lower()

    try:
        with open(raw_log_path, "a", encoding="utf-8") as f:
            f.write(f"{who}: {clean_txt}\n")
    except OSError:
        return False

    msg_cache = _recent_msg_by_id.setdefault(gid, {})
    if message_id:
        msg_cache[str(message_id)] = {"uid": uid, "name": who, "text": clean_txt, "ts": now_ts}
        if len(msg_cache) > 300:
            msg_cache.pop(next(iter(msg_cache)), None)

    if is_luis or (2 <= len(clean_txt) <= 12):
        _append_short_reply_pool(gid, clean_txt)

    # 仅使用真实引用关系构建问答样本，绝不按 90s 相邻时间戳盲配！
    prompt_txt = _clean_group_text(reply_to_text)
    if not prompt_txt and reply_to_message_id and str(reply_to_message_id) in msg_cache:
        ref_item = msg_cache[str(reply_to_message_id)]
        if ref_item.get("uid") != uid:
            prompt_txt = str(ref_item.get("text") or "")

    # 同步录入 MaiBot 场景表达学习缓冲池
    expression_store.record_verified_turn_for_learning(
        group_id=gid,
        message_id=str(message_id or ""),
        sender_uid=uid,
        sender_name=who,
        text=clean_txt,
        reply_to_message_id=reply_to_message_id,
        reply_to_text=prompt_txt,
        bot_self_id=bot_self_id,
        timestamp=float(now_ts),
    )

    if prompt_txt and prompt_txt != clean_txt:
        sft_record = {
            "group_id": gid,
            "ts": now_ts,
            "responder": who,
            "has_explicit_reply": True,
            "is_luis_style": is_luis or len(clean_txt) <= 12,
            "messages": [
                {"role": "system", "content": f"你是阿米娅，在QQ群 {gid} 中保持自身温柔性格并自然借用群内轻快表达接话。"},
                {"role": "user", "content": prompt_txt},
                {"role": "assistant", "content": clean_txt},
            ],
        }
        try:
            with open(dataset_path, "a", encoding="utf-8") as df:
                df.write(json.dumps(sft_record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    count = _msg_counter_since_train.get(gid, 0) + 1
    _msg_counter_since_train[gid] = count

    st = _training_stats.setdefault(gid, {"collected_lines": 0, "dataset_pairs": 0, "last_trained_at": "", "status": "idle"})
    st["collected_lines"] = int(st.get("collected_lines", 0)) + 1

    if count >= AUTO_TRAIN_EVERY_N_MSGS:
        _msg_counter_since_train[gid] = 0
        try:
            asyncio.get_running_loop()
            training_job_manager.submit_job(gid, lambda: trigger_group_training_now(gid), kind="auto_group_training")
        except RuntimeError:
            pass
    return True


def _read_training_files_sync(raw_log_path: Path, dataset_path: Path) -> tuple[str, int]:
    """在工作线程中读取训练日志并统计配对行数，避免阻塞事件循环。"""
    raw_text = raw_log_path.read_text(encoding="utf-8", errors="ignore") if raw_log_path.is_file() else ""
    pair_count = 0
    if dataset_path.is_file():
        try:
            with dataset_path.open("r", encoding="utf-8", errors="ignore") as f:
                pair_count = sum(1 for _ in f)
        except OSError:
            pass
    return raw_text, pair_count


async def trigger_group_training_now(group_id: str) -> dict[str, Any]:
    """根据已采集的群聊语料训练更新 data/groups/{group_id}.json 与场景表达库。
    严格区分成功 (`status="ok"`) 与失败 (`status="error"` | `"empty"`)，绝不把失败记录为“训练完成”。
    """
    gid = str(group_id or "").strip()
    if not gid:
        return {"status": "error", "message": "缺少群号"}

    lock = _training_locks.setdefault(gid, asyncio.Lock())
    if lock.locked():
        return {"status": "busy", "group_id": gid, "message": "该群正在训练中"}

    async with lock:
        raw_log_path = TRAINING_DIR / f"{gid}_raw.log"
        dataset_path = TRAINING_DIR / f"{gid}_dataset.jsonl"
        if not raw_log_path.is_file():
            err_res = {"status": "empty", "group_id": gid, "message": "尚无已采集的该群聊天记录"}
            _training_stats[gid] = {
                **_training_stats.get(gid, {}),
                "group_id": gid,
                "status": "empty",
                "last_error": err_res["message"],
            }
            return err_res

        raw_text, pair_count = await asyncio.to_thread(_read_training_files_sync, raw_log_path, dataset_path)
        res = await log_extractor.run_log_extraction(gid, raw_text)
        res_status = str(res.get("status") or "error")
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")

        if res_status != "ok":
            err_msg = str(res.get("message") or "群风格提炼失败")
            _training_stats[gid] = {
                "group_id": gid,
                "collected_lines": res.get("lines", 0),
                "dataset_pairs": pair_count,
                "last_trained_at": _training_stats.get(gid, {}).get("last_trained_at", ""),
                "status": "error",
                "last_error": err_msg,
                "system_prompt_preview": "",
            }
            logger.warning(f"[训练模式] 群 {gid} 训练失败: {err_msg}")
            return {**res, "status": "error", "message": err_msg, "dataset_pairs": pair_count}

        _training_stats[gid] = {
            "group_id": gid,
            "collected_lines": res.get("lines", 0),
            "dataset_pairs": pair_count,
            "last_trained_at": now_str,
            "status": "ok",
            "last_error": "",
            "system_prompt_preview": res.get("system_prompt_preview", ""),
            "learned_expressions": res.get("learned_expressions", 0),
        }
        logger.info(f"[训练模式] 群 {gid} 提炼成功: lines={res.get('lines')}, pairs={pair_count}, exprs={res.get('learned_expressions', 0)}")
        return {**res, "dataset_pairs": pair_count, "last_trained_at": now_str}


async def fetch_and_train_group_history(bot: Any, group_id: str, count: int = 120) -> dict[str, Any]:
    """主动通过 OneBot get_group_msg_history 拉取指定群历史消息，仅对真实引用关系配对并执行提炼。"""
    gid = str(group_id or "").strip()
    if not gid.isdigit():
        return {"status": "error", "message": f"无效群号: {gid}"}

    fetched = 0
    if bot is not None:
        try:
            self_id = str(getattr(bot, "self_id", "") or "")
            resp = await asyncio.wait_for(
                bot.call_api("get_group_msg_history", group_id=int(gid), count=int(count)),
                timeout=12.0,
            )
            msgs = (resp.get("messages") if isinstance(resp, dict) else None) or []
            if isinstance(msgs, list):
                TRAINING_DIR.mkdir(parents=True, exist_ok=True)
                raw_log_path = TRAINING_DIR / f"{gid}_raw.log"
                dataset_path = TRAINING_DIR / f"{gid}_dataset.jsonl"
                by_msg_id: dict[str, dict[str, str]] = {}

                def _write_fetched_sync() -> int:
                    cnt = 0
                    with open(raw_log_path, "a", encoding="utf-8") as rf, open(dataset_path, "a", encoding="utf-8") as df:
                        for m in msgs:
                            if not isinstance(m, dict):
                                continue
                            sender = m.get("sender") or {}
                            suid = str(sender.get("user_id") or m.get("user_id") or "").strip()
                            if self_id and suid == self_id:
                                continue
                            sname = str(sender.get("card") or sender.get("nickname") or suid or "群友").strip()
                            if any(b in sname.lower() for b in _BOT_NAMES):
                                continue
                            mid = str(m.get("message_id") or "").strip()
                            raw_m = m.get("raw_message") or ""
                            reply_id = ""
                            if isinstance(m.get("message"), list):
                                for seg in m["message"]:
                                    if isinstance(seg, dict) and seg.get("type") == "reply":
                                        reply_id = str(seg.get("data", {}).get("id") or "").strip()
                                if not raw_m:
                                    raw_m = " ".join(
                                        str(seg.get("data", {}).get("text") or "")
                                        for seg in m["message"]
                                        if isinstance(seg, dict) and seg.get("type") == "text"
                                    )
                            if not reply_id and raw_m:
                                m_rep = re.search(r"\[CQ:reply,id=(\d+)\]", str(raw_m))
                                if m_rep:
                                    reply_id = m_rep.group(1)
                            clean_t = _clean_group_text(str(raw_m))
                            if not clean_t:
                                continue
                            rf.write(f"{sname}: {clean_t}\n")
                            cnt += 1
                            if mid:
                                by_msg_id[mid] = {"uid": suid, "name": sname, "text": clean_t}
                            if "路易斯" in sname.lower() or (2 <= len(clean_t) <= 12):
                                _append_short_reply_pool(gid, clean_t)
                            # 仅当存在明确 reply_id 且指向非本人消息时才写入 SFT 对话对
                            if reply_id and reply_id in by_msg_id:
                                target_m = by_msg_id[reply_id]
                                if target_m["uid"] != suid and target_m["text"] != clean_t:
                                    rec = {
                                        "group_id": gid,
                                        "ts": int(m.get("time") or time.time()),
                                        "responder": sname,
                                        "has_explicit_reply": True,
                                        "is_luis_style": ("路易斯" in sname.lower()) or len(clean_t) <= 12,
                                        "messages": [
                                            {"role": "system", "content": f"你是阿米娅，在QQ群 {gid} 中保持自身温柔性格并自然借用群内轻快表达接话。"},
                                            {"role": "user", "content": target_m["text"]},
                                            {"role": "assistant", "content": clean_t},
                                        ],
                                    }
                                    df.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    return cnt

                fetched = await asyncio.to_thread(_write_fetched_sync)
        except Exception as exc:
            logger.warning(f"[训练模式] 拉取群 {gid} 历史消息跳过: {exc}")

    train_res = await trigger_group_training_now(gid)
    train_res["fetched_history_count"] = fetched
    return train_res


def _scan_training_status_sync() -> dict[str, Any]:
    """在工作线程中扫描训练目录统计信息。"""
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    GROUPS_DIR.mkdir(parents=True, exist_ok=True)
    groups_summary: list[dict[str, Any]] = []
    target_gids = list(dict.fromkeys([str(g) for g in (state.training_groups or state.active_groups or [])]))
    for p in sorted(TRAINING_DIR.glob("*_raw.log")):
        gid = p.name.removesuffix("_raw.log")
        if gid not in target_gids:
            target_gids.append(gid)
    for p in sorted(GROUPS_DIR.glob("*.json")):
        gid = p.stem
        if gid.isdigit() and gid not in target_gids:
            target_gids.append(gid)

    for gid in target_gids:
        raw_p = TRAINING_DIR / f"{gid}_raw.log"
        ds_p = TRAINING_DIR / f"{gid}_dataset.jsonl"
        prof_p = GROUPS_DIR / f"{gid}.json"
        lines_cnt = 0
        pairs_cnt = 0
        if raw_p.is_file():
            try:
                with raw_p.open("r", encoding="utf-8", errors="ignore") as f:
                    lines_cnt = sum(1 for _ in f)
            except OSError:
                pass
        if ds_p.is_file():
            try:
                with ds_p.open("r", encoding="utf-8", errors="ignore") as f:
                    pairs_cnt = sum(1 for _ in f)
            except OSError:
                pass
        st_info = _training_stats.get(gid, {})
        expr_cnt = len(expression_store.load_group_expressions(gid))
        groups_summary.append(
            {
                "group_id": gid,
                "raw_lines": lines_cnt,
                "dataset_pairs": pairs_cnt,
                "scene_expressions": expr_cnt,
                "has_trained_profile": prof_p.is_file(),
                "status": st_info.get("status", "ok" if prof_p.is_file() else "idle"),
                "last_error": st_info.get("last_error", ""),
                "last_trained_at": st_info.get("last_trained_at", ""),
            }
        )
    return {
        "training_mode": state.training_mode,
        "small_talk_mode": state.small_talk_mode,
        "training_groups": state.training_groups,
        "groups": groups_summary,
        "recent_jobs": training_job_manager.list_jobs(limit=10),
    }


def get_training_status() -> dict[str, Any]:
    """获取当前训练模式状态、各群数据集条数、场景表达数与后台任务列表。"""
    return _scan_training_status_sync()


async def get_training_status_async() -> dict[str, Any]:
    """异步非阻塞获取训练状态。"""
    return await asyncio.to_thread(_scan_training_status_sync)
