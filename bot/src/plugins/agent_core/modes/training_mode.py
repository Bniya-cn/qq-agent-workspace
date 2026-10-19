"""训练模式（Training Mode）：自动读取指定 QQ 群的所有消息，构建 SFT 微调数据集 (.jsonl) 并自动训练更新群专属提示词与短句池。"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from nonebot import logger

from ..config_manager import BOT_ROOT, state
from .. import log_extractor

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
)

# 内存跟踪每个群最近一条群友消息，用于自动配对构建 (user -> assistant) 对话语料
_last_group_turn: dict[str, dict[str, Any]] = {}
_msg_counter_since_train: dict[str, int] = {}
_training_locks: dict[str, asyncio.Lock] = {}
_training_stats: dict[str, dict[str, Any]] = {}


def is_training_group(group_id: Optional[str]) -> bool:
    """判断指定群是否开启了自动采集与训练。"""
    if not group_id or not state.training_mode:
        return False
    gid = str(group_id).strip()
    targets = {str(g).strip() for g in (state.training_groups or []) if str(g).strip()}
    if targets:
        return gid in targets
    # 若开启了 training_mode 但未单独指定 training_groups，默认对白名单群生效
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
    return s


def _append_short_reply_pool(group_id: str, text: str) -> None:
    """将群内 2~14 字的自然短句（含“路易斯”风格接话）归入短句池，供闲聊模式与 Few-Shot 采样。"""
    if not (2 <= len(text) <= 14):
        return
    if re.search(r"https?://|[0-9]{7,}", text):
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
) -> bool:
    """实时记录群聊消息到训练语料库与 SFT 数据集 (.jsonl)，达到阈值时后台自动触发提示词提炼。"""
    gid = str(group_id or "").strip()
    if not is_training_group(gid):
        return False

    clean_txt = _clean_group_text(raw_text)
    if not clean_txt:
        return False

    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    raw_log_path = TRAINING_DIR / f"{gid}_raw.log"
    dataset_path = TRAINING_DIR / f"{gid}_dataset.jsonl"

    now_ts = int(time.time())
    who = (sender_name or sender_uid or "群友").strip()
    is_luis = "路易斯" in who.lower()

    try:
        with open(raw_log_path, "a", encoding="utf-8") as f:
            f.write(f"{who}: {clean_txt}\n")
    except OSError:
        return False

    # 若发送者昵称含“路易斯”或是精炼短句，优先收入短句池
    if is_luis or (2 <= len(clean_txt) <= 12):
        _append_short_reply_pool(gid, clean_txt)

    # 构建 SFT 对话对：优先使用显式引用的目标文本，否则配对群内 90 秒内的上一条他人的消息
    prev = _last_group_turn.get(gid)
    prompt_txt = _clean_group_text(reply_to_text)
    if not prompt_txt and prev and prev.get("uid") != sender_uid and (now_ts - int(prev.get("ts", 0))) <= 90:
        prompt_txt = str(prev.get("text") or "")

    if prompt_txt and prompt_txt != clean_txt:
        sft_record = {
            "group_id": gid,
            "ts": now_ts,
            "responder": who,
            "is_luis_style": is_luis or len(clean_txt) <= 12,
            "messages": [
                {"role": "system", "content": f"你是QQ群 {gid} 的活跃群友，用简短自然的群内口吻接话。"},
                {"role": "user", "content": prompt_txt},
                {"role": "assistant", "content": clean_txt},
            ],
        }
        try:
            with open(dataset_path, "a", encoding="utf-8") as df:
                df.write(json.dumps(sft_record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    _last_group_turn[gid] = {"uid": sender_uid, "name": who, "text": clean_txt, "ts": now_ts}
    count = _msg_counter_since_train.get(gid, 0) + 1
    _msg_counter_since_train[gid] = count

    st = _training_stats.setdefault(gid, {"collected_lines": 0, "dataset_pairs": 0, "last_trained_at": ""})
    st["collected_lines"] = int(st.get("collected_lines", 0)) + 1

    # 每满 AUTO_TRAIN_EVERY_N_MSGS 条新消息，后台自动触发一次群提示词与人设训练
    if count >= AUTO_TRAIN_EVERY_N_MSGS:
        _msg_counter_since_train[gid] = 0
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(trigger_group_training_now(gid))
        except RuntimeError:
            pass
    return True


async def trigger_group_training_now(group_id: str) -> dict[str, Any]:
    """根据已采集的群聊语料与短句池，立即训练并更新 data/groups/{group_id}.json 提示词。"""
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
            return {"status": "empty", "group_id": gid, "message": "尚无已采集的该群聊天记录"}

        raw_text = raw_log_path.read_text(encoding="utf-8", errors="ignore")
        res = await log_extractor.run_log_extraction(gid, raw_text)

        pair_count = 0
        if dataset_path.is_file():
            try:
                pair_count = sum(1 for _ in dataset_path.open("r", encoding="utf-8"))
            except OSError:
                pass

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        _training_stats[gid] = {
            "group_id": gid,
            "collected_lines": res.get("lines", 0),
            "dataset_pairs": pair_count,
            "last_trained_at": now_str,
            "status": res.get("status", "ok"),
            "system_prompt_preview": res.get("system_prompt_preview", ""),
        }
        logger.info(f"[训练模式] 群 {gid} 数据集与提示词训练完成: lines={res.get('lines')}, pairs={pair_count}")
        return {**res, "dataset_pairs": pair_count, "last_trained_at": now_str}


async def fetch_and_train_group_history(bot: Any, group_id: str, count: int = 120) -> dict[str, Any]:
    """主动通过 OneBot get_group_msg_history 拉取指定群的历史消息，写入数据集并立即训练提示词。"""
    gid = str(group_id or "").strip()
    if not gid.isdigit():
        return {"status": "error", "message": f"无效群号: {gid}"}

    fetched = 0
    if bot is not None:
        try:
            resp = await asyncio.wait_for(
                bot.call_api("get_group_msg_history", group_id=int(gid), count=int(count)),
                timeout=12.0,
            )
            msgs = (resp.get("messages") if isinstance(resp, dict) else None) or []
            if isinstance(msgs, list):
                TRAINING_DIR.mkdir(parents=True, exist_ok=True)
                raw_log_path = TRAINING_DIR / f"{gid}_raw.log"
                dataset_path = TRAINING_DIR / f"{gid}_dataset.jsonl"
                prev_txt = ""
                prev_uid = ""
                with open(raw_log_path, "a", encoding="utf-8") as rf, open(dataset_path, "a", encoding="utf-8") as df:
                    for m in msgs:
                        if not isinstance(m, dict):
                            continue
                        sender = m.get("sender") or {}
                        suid = str(sender.get("user_id") or m.get("user_id") or "").strip()
                        sname = str(sender.get("card") or sender.get("nickname") or suid or "群友").strip()
                        raw_m = m.get("raw_message") or ""
                        if not raw_m and isinstance(m.get("message"), list):
                            raw_m = " ".join(
                                str(seg.get("data", {}).get("text") or "")
                                for seg in m["message"]
                                if isinstance(seg, dict) and seg.get("type") == "text"
                            )
                        clean_t = _clean_group_text(str(raw_m))
                        if not clean_t:
                            continue
                        rf.write(f"{sname}: {clean_t}\n")
                        fetched += 1
                        if "路易斯" in sname.lower() or (2 <= len(clean_t) <= 12):
                            _append_short_reply_pool(gid, clean_t)
                        if prev_txt and prev_uid != suid and prev_txt != clean_t:
                            rec = {
                                "group_id": gid,
                                "ts": int(m.get("time") or time.time()),
                                "responder": sname,
                                "is_luis_style": ("路易斯" in sname.lower()) or len(clean_t) <= 12,
                                "messages": [
                                    {"role": "system", "content": f"你是QQ群 {gid} 的活跃群友，用简短自然的群内口吻接话。"},
                                    {"role": "user", "content": prev_txt},
                                    {"role": "assistant", "content": clean_t},
                                ],
                            }
                            df.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        prev_txt = clean_t
                        prev_uid = suid
        except Exception as exc:
            logger.warning(f"[训练模式] 拉取群 {gid} 历史消息跳过: {exc}")

    train_res = await trigger_group_training_now(gid)
    train_res["fetched_history_count"] = fetched
    return train_res


def get_training_status() -> dict[str, Any]:
    """获取当前训练模式状态、各群数据集条数与已生成的群画像列表。"""
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    GROUPS_DIR.mkdir(parents=True, exist_ok=True)
    groups_summary: list[dict[str, Any]] = []
    target_gids = list(dict.fromkeys([str(g) for g in (state.training_groups or state.active_groups or [])]))
    for p in sorted(TRAINING_DIR.glob("*_raw.log")):
        gid = p.name.removesuffix("_raw.log")
        if gid not in target_gids:
            target_gids.append(gid)

    for gid in target_gids:
        raw_p = TRAINING_DIR / f"{gid}_raw.log"
        ds_p = TRAINING_DIR / f"{gid}_dataset.jsonl"
        prof_p = GROUPS_DIR / f"{gid}.json"
        lines_cnt = 0
        pairs_cnt = 0
        if raw_p.is_file():
            try:
                lines_cnt = sum(1 for _ in raw_p.open("r", encoding="utf-8", errors="ignore"))
            except OSError:
                pass
        if ds_p.is_file():
            try:
                pairs_cnt = sum(1 for _ in ds_p.open("r", encoding="utf-8", errors="ignore"))
            except OSError:
                pass
        groups_summary.append(
            {
                "group_id": gid,
                "raw_lines": lines_cnt,
                "dataset_pairs": pairs_cnt,
                "has_trained_profile": prof_p.is_file(),
                "last_trained_at": _training_stats.get(gid, {}).get("last_trained_at", ""),
            }
        )
    return {
        "training_mode": state.training_mode,
        "small_talk_mode": state.small_talk_mode,
        "training_groups": state.training_groups,
        "groups": groups_summary,
    }
