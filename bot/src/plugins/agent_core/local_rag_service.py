"""本地知识检索、快速对话与带回执状态的每日发布服务；前端和生命周期由控制器注册。"""
from __future__ import annotations

import asyncio
import base64
import collections
from contextvars import ContextVar
import datetime
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import time
import urllib.parse
import tempfile
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Literal
from zoneinfo import ZoneInfo
from uuid import uuid4
from nonebot import logger
from nonebot.adapters.onebot.v11.exception import ActionFailed, NetworkError

from .config_manager import BOT_ROOT, ensure_bot_env, state
from . import conversation_service as conversation
from .modes import small_talk
from . import situational_state

VECTOR_DB_PATH = BOT_ROOT / "data" / "vector_kb.sqlite3"
SCHEDULE_STATE_PATH = BOT_ROOT / "data" / "daily_schedule_state.json"
QZONE_HISTORY_PATH = BOT_ROOT / "data" / "qzone_posts.json"
VECTOR_DIM = 256

# 保持长连接复用池，消除每次请求 250~400ms 的 DNS + TCP + TLS 握手开销
_WARM_HTTP_CLIENT: Optional[httpx.AsyncClient] = None

# 记录群聊多人连续发言时间轴（毫秒级同步存储）：group_id -> deque[(mono_ts, time_str, msg_id, uid, sender_name, text)]
_GROUP_TIMELINE: dict[str, collections.deque[dict[str, Any]]] = {}

# 记录当前协程正在处理的原始 Event 信息（用于精准引用回复与时间轴对齐）
_CURRENT_MSG_META: ContextVar[Optional[dict[str, Any]]] = ContextVar("qq_current_msg_meta", default=None)

# 最近极速引擎耗时统计（供 WebUI 实时展示）
_LATENCY_METRICS: collections.deque[dict[str, Any]] = collections.deque(maxlen=30)

_DAILY_SCHEDULER_TASK: Optional[asyncio.Task] = None
_DAILY_JOBS: dict[str, dict[str, Any]] = {}
_DAILY_TASKS: dict[str, asyncio.Task] = {}
_DAILY_TIMEZONE = ZoneInfo("Asia/Shanghai")
_RAG_INITIALIZATION_ERROR = ""
# qwen-image-3.0-pro 免费额度已耗尽（403 AllocationQuota.FreeTierOnly），暂用可用的 2.1-pro。
DAILY_IMAGE_MODEL = "qwen-image-3.0-pro"
DAILY_IMAGE_TIMEOUT_SEC = 240.0
DAILY_SCHEDULE_CATCHUP_SECONDS = 5 * 60

# 内置去人机感、鲜活角色与高频梗种子知识库
_SEED_KNOWLEDGE: list[dict[str, str]] = [
    {
        "character": "amiya",
        "category": "persona_core",
        "title": "阿米娅群聊去人机口吻准则",
        "content": "阿米娅是罗德岛领袖，温柔真诚、体恤他人而坚定负责。称呼博士，认真回应眼前的具体问题；不强行接梗，不套用固定关心台词。",
        "keywords": "阿米娅 性格 口吻 人设 聊天 博士 兔兔",
    },
    {
        "character": "amiya",
        "category": "gaming_meme",
        "title": "游戏发售与颁奖联想（GTA6 / TGA / Steam / 抽卡）",
        "content": "聊到TGA提名截止（如11月20日）与大作发售（如GTA6定档11月19日）时，立刻联想到TGA卡点想蹭GTA6热度占便宜；聊到抽卡手痒、歪卡、囤石时，用真实玩家口吻调侃别上头或毒奶。",
        "keywords": "tga gta6 发售 提名 抽卡 囤石 保底 歪了 游戏 鸣潮 明日方舟",
    },
    {
        "character": "amiya",
        "category": "daily_banter",
        "title": "日常吐槽与校园职场接话",
        "content": "群友吐槽食堂难吃、早八困、考公行测申论、加班摸鱼、凹分手痒时，像真实熟人一样顺嘴接梗或跟着吐槽，给实在又接地气的反应，不说官话套话。",
        "keywords": "食堂 难吃 早八 考公 摸鱼 凹分 困 饿 奶茶 夜宵 周末",
    },
    {
        "character": "amiya",
        "category": "lore_light",
        "title": "阿米娅背景设定（仅在被问及设定时自然提及）",
        "content": "阿米娅是卡特斯（兔子）少女，会拉小提琴，喜欢和博士并肩作战，在大家面前其实很有少女心，偶尔被叫驴驴会小声抗议自己是兔子。",
        "keywords": "小提琴 卡特斯 兔子 驴驴 阿米驴 设定 原型",
    },
    {
        "character": "dania",
        "category": "persona_core",
        "title": "达妮娅傲娇战友口吻",
        "content": "达妮娅是《鸣潮》拉海洛地区的共鸣者，傲娇嘴硬心软，称呼对方为漂泊者或你，表面哼一声嫌麻烦，实际立刻帮忙，绝不劝人早睡，喜欢美食和热闹。",
        "keywords": "达妮娅 达尼亚 漂泊者 傲娇 鸣潮 拉海洛",
    },
    {
        "character": "kaltsit",
        "category": "persona_core",
        "title": "凯尔希冷淡毒舌口吻",
        "content": "凯尔希冷静克制、言简意赅、带一点老搭档的毒舌与默契，与Mon3tr形影不离，一两句话直接点破问题核心，从不煽情或长篇说教。",
        "keywords": "凯尔希 老女人 医生 Mon3tr 毒舌 前妻",
    },
]

LEGACY_AMIYA_CORE = '阿米娅在群里是机灵、俏皮、懂梗的同龄女生朋友，称呼博士或顺着群友话头接茬。日常闲聊绝不端罗德岛领袖架子，不提公文、甲板吹风、热红茶或早点休息，说话直奔槽点，一句到位。'

_TAROT_DECK = [
    ("愚者·正位", "尝试新点子、顺心摸鱼", "钻牛角尖、胡思乱想"),
    ("魔术师·正位", "推进计划、灵感拉满", "拖延症发作、半途而废"),
    ("女祭司·正位", "安静刷题、稳扎稳打", "冲动消费、跟人抬杠"),
    ("太阳·正位", "十连出金、吃顿好的", "深夜emo、空腹喝冰美式"),
    ("星星·正位", "许愿囤石、听歌放松", "熬夜爆肝、替人背锅"),
    ("命运之轮·正位", "接住好运、清理杂事", "犹豫不决、死磕烂代码"),
    ("战车·正位", "速战速决、出门透透气", "赖床不起、三分钟热度"),
    ("力量·正位", "稳住心态、攻克难关", "暴饮暴食、乱立flag"),
    ("隐士·正位", "独处充电、整理心情", "无效社交、盲目跟风"),
    ("世界·正位", "完美收官、犒劳自己", "节外生枝、苛求完美"),
]


def _get_warm_client() -> httpx.AsyncClient:
    global _WARM_HTTP_CLIENT
    if _WARM_HTTP_CLIENT is None or _WARM_HTTP_CLIENT.is_closed:
        _WARM_HTTP_CLIENT = httpx.AsyncClient(
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=12, max_connections=24, keepalive_expiry=90.0),
            timeout=httpx.Timeout(connect=1.5, read=3.5, write=2.0, pool=1.5),
        )
    return _WARM_HTTP_CLIENT


def _is_valid_image_bytes(raw: Optional[bytes]) -> bool:
    """严格自测图片二进制魔数与长度，防止错误 JSON/HTML 或损坏数据被当成图片发出。"""
    if not raw or len(raw) < 2048:
        return False
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if raw[:3] == b"\xff\xd8\xff":
        return True
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return True
    if raw[:4] == b"GIF8":
        return True
    return False


def compute_local_vector(text: str, dim: int = VECTOR_DIM) -> list[float]:
    """零网络延迟的 256 维有符号特征哈希向量（融合字符 Unigram / Bigram / Trigram 与词元权重，耗时 <0.3ms）。"""
    vec = [0.0] * dim
    s = re.sub(r"\s+", " ", (text or "").strip().lower())
    if not s:
        return vec

    features: list[tuple[str, float]] = []
    clean_chars = re.sub(r"[^\w\u4e00-\u9fff]", "", s)
    n = len(clean_chars)
    for i in range(n):
        features.append((f"c1:{clean_chars[i]}", 0.6))
        if i + 1 < n:
            features.append((f"c2:{clean_chars[i:i+2]}", 1.4))
        if i + 2 < n:
            features.append((f"c3:{clean_chars[i:i+3]}", 1.8))

    for tok in re.findall(r"[a-z0-9_]{2,}", s):
        features.append((f"w:{tok}", 2.2))

    tf_counts: collections.Counter[str] = collections.Counter()
    weights: dict[str, float] = {}
    for feat, w in features:
        tf_counts[feat] += 1
        weights[feat] = w

    for feat, count in tf_counts.items():
        h = int(hashlib.md5(feat.encode("utf-8")).hexdigest()[:8], 16)
        idx = h % dim
        sign = 1.0 if ((h >> 8) & 1) == 0 else -1.0
        val = (1.0 + math.log(count)) * weights[feat]
        vec[idx] += sign * val

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 1e-8:
        vec = [round(v / norm, 5) for v in vec]
    return vec


async def compute_dashscope_embedding(text: str, dim: int = VECTOR_DIM) -> Optional[list[float]]:
    """已切换为纯本地向量检索，不再调用阿里百炼云端 Embedding。"""
    return None


def _load_local_knowledge_file_chunks() -> list[dict[str, str]]:
    """从 bot/data/knowledge/ 自动读取本地干员档案与语音表切片，灌入本地 SQLite 向量库。"""
    kb_dir = BOT_ROOT / "data" / "knowledge"
    chunks: list[dict[str, str]] = []
    if not kb_dir.is_dir():
        return chunks
    handbook_specs = [
        ("amiya", kb_dir / "amiya_handbook.txt", "阿米娅档案"),
        ("kaltsit", kb_dir / "kaltsit_handbook.txt", "凯尔希档案"),
    ]
    for char, path, prefix in handbook_specs:
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            sections = re.split(r"\n(?=##\s+|###\s+|【)", raw)
            for sec in sections:
                s = sec.strip()
                if len(s) < 20:
                    continue
                first_line = s.splitlines()[0].lstrip("#【】 ").strip()[:36] or prefix
                body = " ".join(line.strip() for line in s.splitlines()[1:] if line.strip())[:420] or s[:420]
                chunks.append({
                    "character": char,
                    "category": "handbook_lore",
                    "title": f"{prefix}·{first_line}",
                    "content": body,
                    "keywords": f"{char} {first_line} 档案 设定 罗德岛",
                })
        except Exception:
            continue
    return chunks[:40]


def _cosine_sim(v1: list[float], v2: list[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    return sum(a * b for a, b in zip(v1, v2))


class LocalVectorKnowledgeBase:
    """基于 SQLite 的本地向量 + 关键词混合知识库（检索耗时 <1.5ms）。"""

    def __init__(self, db_path: Path = VECTOR_DB_PATH) -> None:
        self.db_path = db_path
        self._cache: list[dict[str, Any]] = []
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kb_chunks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "character TEXT NOT NULL, "
            "category TEXT NOT NULL, "
            "title TEXT NOT NULL, "
            "content TEXT NOT NULL, "
            "keywords TEXT NOT NULL, "
            "local_vec TEXT NOT NULL, "
            "cloud_vec TEXT DEFAULT '', "
            "updated_at TEXT NOT NULL)"
        )
        return conn

    def ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
            if count == 0:
                now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for item in _SEED_KNOWLEDGE:
                    combined = f"{item['title']} {item['keywords']} {item['content']}"
                    l_vec = json.dumps(compute_local_vector(combined))
                    conn.execute(
                        "INSERT INTO kb_chunks (character, category, title, content, keywords, local_vec, cloud_vec, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, '', ?)",
                        (
                            item["character"],
                            item["category"],
                            item["title"],
                            item["content"],
                            item["keywords"],
                            l_vec,
                            now_str,
                        ),
                    )
                conn.commit()
            # 只修复与旧内置内容完全一致的记录，保留用户自定义与手工编辑。
            old_content = LEGACY_AMIYA_CORE
            seed = _SEED_KNOWLEDGE[0]
            vector = json.dumps(compute_local_vector(f"{seed['title']} {seed['keywords']} {seed['content']}"))
            conn.execute("UPDATE kb_chunks SET content=?, local_vec=?, cloud_vec='' WHERE character='amiya' AND category='persona_core' AND title=? AND content=?", (seed['content'], vector, seed['title'], old_content))
            existing_titles = {r[0] for r in conn.execute("SELECT title FROM kb_chunks").fetchall()}
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for item in _load_local_knowledge_file_chunks():
                if item["title"] in existing_titles:
                    continue
                combined = f"{item['title']} {item['keywords']} {item['content']}"
                l_vec = json.dumps(compute_local_vector(combined))
                conn.execute(
                    "INSERT INTO kb_chunks (character, category, title, content, keywords, local_vec, cloud_vec, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, '', ?)",
                    (
                        item["character"],
                        item["category"],
                        item["title"],
                        item["content"],
                        item["keywords"],
                        l_vec,
                        now_str,
                    ),
                )
            conn.commit()
        self.reload_memory_cache()
        self._initialized = True

    def reload_memory_cache(self) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, character, category, title, content, keywords, local_vec, cloud_vec, updated_at "
                "FROM kb_chunks ORDER BY id DESC"
            ).fetchall()
        cache: list[dict[str, Any]] = []
        for r in rows:
            try:
                l_vec = json.loads(r[6]) if r[6] else []
            except Exception:
                l_vec = []
            try:
                c_vec = json.loads(r[7]) if r[7] else []
            except Exception:
                c_vec = []
            cache.append({
                "id": r[0],
                "character": r[1],
                "category": r[2],
                "title": r[3],
                "content": r[4],
                "keywords": r[5],
                "local_vec": l_vec,
                "cloud_vec": c_vec,
                "has_cloud_vec": bool(c_vec),
                "updated_at": r[8],
            })
        self._cache = cache

    async def add_entry(
        self,
        *,
        character: str,
        title: str,
        content: str,
        keywords: str = "",
        category: str = "custom",
    ) -> dict[str, Any]:
        self.ensure_initialized()
        char = (character or "amiya").strip().lower()
        combined = f"{title} {keywords} {content}"
        l_vec = compute_local_vector(combined)
        c_vec: Optional[list[float]] = None
        if state.vector_engine_mode in {"dashscope", "hybrid"}:
            c_vec = await compute_dashscope_embedding(combined)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO kb_chunks (character, category, title, content, keywords, local_vec, cloud_vec, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    char,
                    category,
                    title.strip(),
                    content.strip(),
                    keywords.strip(),
                    json.dumps(l_vec),
                    json.dumps(c_vec) if c_vec else "",
                    now_str,
                ),
            )
            new_id = cur.lastrowid
            conn.commit()
        self.reload_memory_cache()
        return {"id": new_id, "character": char, "title": title, "has_cloud_vec": bool(c_vec)}

    def delete_entry(self, entry_id: int) -> bool:
        self.ensure_initialized()
        with self._connect() as conn:
            changed = conn.execute("DELETE FROM kb_chunks WHERE id = ?", (int(entry_id),)).rowcount
            conn.commit()
        self.reload_memory_cache()
        return changed > 0

    def search_sync(
        self,
        query: str,
        character: str = "amiya",
        *,
        top_k: int = 2,
    ) -> tuple[list[dict[str, Any]], float]:
        """纯本地毫秒级向量 + 关键词混合检索：返回 (命中条目列表, 检索耗时ms)。"""
        t0 = time.perf_counter()
        self.ensure_initialized()
        q = (query or "").strip()
        if not q or not self._cache:
            return [], 0.0

        char = (character or "amiya").strip().lower()
        q_local_vec = compute_local_vector(q)
        q_lower = q.lower()
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in self._cache:
            if item["character"] not in (char, "all", "common"):
                continue
            group_mimic = bool(state.small_talk_mode and getattr(state, "small_talk_style", "pure_persona") == "group_mimic")
            if char == "amiya" and not group_mimic and item["category"] in {"gaming_meme", "daily_banter"}:
                continue
            sim = _cosine_sim(q_local_vec, item["local_vec"])
            kw_boost = 0.0
            for kw in re.split(r"[\s,，、/]+", item["keywords"].lower()):
                if len(kw) >= 2 and kw in q_lower:
                    kw_boost += 0.14
            total_score = round(min(0.99, sim + min(0.35, kw_boost)), 4)
            if total_score >= 0.12 or item["category"] == "persona_core":
                scored.append((total_score, {**item, "score": total_score}))

        scored.sort(key=lambda x: x[0], reverse=True)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        results = [
            {
                "id": it["id"],
                "character": it["character"],
                "category": it["category"],
                "title": it["title"],
                "content": it["content"],
                "keywords": it["keywords"],
                "score": it["score"],
                "has_cloud_vec": it["has_cloud_vec"],
            }
            for _, it in scored[:top_k]
        ]
        return results, elapsed_ms

    async def search(
        self,
        query: str,
        character: str = "amiya",
        *,
        top_k: int = 2,
        allow_cloud_embedding: bool = False,
    ) -> tuple[list[dict[str, Any]], float]:
        """异步兼容包装：直接调用本地毫秒级向量检索。"""
        return self.search_sync(query, character=character, top_k=top_k)

    def list_entries(self, character: str = "") -> list[dict[str, Any]]:
        char = (character or "").strip().lower()
        out = []
        for it in self._cache:
            if char and it["character"] != char:
                continue
            out.append({
                "id": it["id"],
                "character": it["character"],
                "category": it["category"],
                "title": it["title"],
                "content": it["content"],
                "keywords": it["keywords"],
                "has_cloud_vec": it["has_cloud_vec"],
                "updated_at": it["updated_at"],
            })
        return out


vector_kb = LocalVectorKnowledgeBase()


# ---------------------------------------------------------------------------
# 群聊多人连续发言时间轴记录与对齐（同步毫秒级存储 + 异步输出防串台）
# ---------------------------------------------------------------------------

def record_group_timeline_event(
    group_id: Optional[str],
    message_id: str,
    uid: str,
    sender_name: str,
    clean_text: str,
) -> dict[str, Any]:
    """消息到达瞬间 (<0.5ms) 同步记录群内发言的时间戳、发送者昵称与 message_id。"""
    now_mono = time.monotonic()
    now_str = datetime.datetime.now().strftime("%H:%M:%S")
    meta = {
        "group_id": str(group_id or ""),
        "message_id": str(message_id or ""),
        "uid": str(uid or ""),
        "sender_name": str(sender_name or uid or "群友"),
        "text": str(clean_text or "").strip(),
        "mono_ts": now_mono,
        "time_str": now_str,
    }
    if group_id and clean_text.strip():
        dq = _GROUP_TIMELINE.setdefault(str(group_id), collections.deque(maxlen=15))
        if not dq or dq[-1].get("message_id") != meta["message_id"] or not meta["message_id"]:
            dq.append(meta)
    return meta


def build_multi_speaker_alignment_hint(group_id: Optional[str], target_meta: Optional[dict[str, Any]]) -> str:
    """当群内短时间有多人连续发言时，构建精准的发言时间轴与目标发言人对齐提示。"""
    if not group_id or not target_meta:
        return ""
    dq = _GROUP_TIMELINE.get(str(group_id))
    if not dq:
        return ""

    now_mono = time.monotonic()
    recent = [m for m in dq if (now_mono - float(m.get("mono_ts", 0))) <= 45.0]
    target_uid = str(target_meta.get("uid") or "").strip()
    target_name = target_meta.get("sender_name") or target_uid or "该群友"
    target_text = target_meta.get("text") or ""
    target_time = target_meta.get("time_str") or ""

    timeline_lines = [
        f"  [{m['time_str']}] {m['sender_name']}: {m['text'][:55]}"
        for m in recent[-5:]
        if str(m.get("text") or "").strip()
    ]

    if not target_text.strip() or "单纯艾特了你" in target_text:
        if not timeline_lines:
            return f"【当前触发人】：{target_name}（单纯艾特了你，请自然打招呼或接话）"
        return (
            "【群聊刚刚的发言脉络】\n"
            + "\n".join(timeline_lines)
            + f"\n👉 注意：【{target_name}】刚刚艾特了你，请紧扣上面最后 1～2 句大家正在聊的最新话题自然接话，严禁翻旧账！"
        )

    if len(timeline_lines) <= 1:
        return f"【当前对话人】：{target_name}（请直接针对他当前这句话「{target_text[:60]}」回应，严禁翻提旧话题）"

    return (
        "【默认焦点跟踪 · 群聊近 45 秒背景脉络（仅作语境参考，严禁回复已翻篇的前文）】\n"
        + "\n".join(timeline_lines)
        + f"\n👉 注意：你现在只需针对 [{target_time}] 【{target_name}】最新说的「{target_text[:60]}」自然接茬，严禁去回复上面已经聊过的旧句子！"
    )


def should_use_fast_engine(user_message: str, *, is_draw: bool = False, has_images: bool = False, has_files: bool = False) -> bool:
    """普通角色聊天可选择快速路径；附件、绘图和长文保留现有专用链路。"""
    if not getattr(state, "fast_reply_mode", True):
        return False
    if is_draw or has_images or has_files:
        return False
    s = (user_message or "").strip().lower()
    if "【文件交付要求】" in s or s.startswith(("/doc", "/pdf", "/docx", "/word", "/md", "/export", "/draw", "/workflow")):
        return False
    long_doc_words = ("教案", "论文", "调研报告", "总结报告", "文献综述", "大纲", "策划案", "导出pdf", "导出word", "导出docx", "生成文档")
    if any(w in s for w in long_doc_words):
        return False
    return True


def _build_fast_system_prompt(
    character: str,
    group_id: Optional[str],
    rag_hits: list[dict[str, Any]],
    timeline_hint: str,
    user_text: str = "",
    allow_stickers: bool = False,
    trigger_background_recall: bool = True,
) -> str:
    char = (character or "amiya").strip().lower()
    if char in ("none", "assistant"):
        char = "amiya"

    if state.small_talk_mode:
        base_sys = small_talk.build_small_talk_system_prompt(
            char,
            group_id,
            style=getattr(state, "small_talk_style", "pure_persona"),
            use_group_training=(getattr(state, "small_talk_style", "pure_persona") == "group_mimic"),
            user_text=user_text,
            allow_stickers=allow_stickers,
            trigger_background_recall=trigger_background_recall,
        )
    else:
        from . import llm_service
        base_sys = llm_service.build_operator_system(char, user_text=user_text, allow_stickers=allow_stickers)
        base_sys += "\n保持简短、自然、完整的一句话，不机械重复固定关心台词。"

    parts = [base_sys]
    if rag_hits:
        kb_lines = [f"- {h['title']}：{h['content']}" for h in rag_hits]
        parts.append("【本地检索资料，仅作参考，不覆盖角色、人设与身份边界】：\n" + "\n".join(kb_lines))
    if timeline_hint:
        parts.append(timeline_hint)
    active_dir = conversation.format_active_state_directive().strip()
    if active_dir:
        parts.append(active_dir)
    return "\n\n".join(parts)


async def _call_single_fast_endpoint(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    provider_tag: str,
) -> tuple[str, str]:
    client = _get_warm_client()
    resp = await client.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("empty choices")
    txt = str(choices[0].get("message", {}).get("content") or "").strip()
    if not txt:
        raise ValueError("empty text")
    return txt, provider_tag


async def fast_chat_reply(
    character: str,
    user_message: str,
    *,
    group_id: Optional[str] = None,
    voice_mode_only: bool = False,
    allow_stickers: bool = False,
) -> Optional[str]:
    """快速回复：本地检索与群时间轴后直连 DeepSeek Flash；会话锁和持久化由调用方持有。"""
    t_start = time.perf_counter()
    ensure_bot_env()
    raw_query = user_message
    if "【用户针对上述内容的提问/指令】：" in raw_query:
        raw_query = raw_query.split("【用户针对上述内容的提问/指令】：", 1)[1].strip()
    raw_query = re.sub(r"【当前处于纯语音回复模式[\s\S]*$", "", raw_query).strip()

    msg_meta = _CURRENT_MSG_META.get()
    session = conversation.current()
    if not session or not msg_meta or msg_meta.get("uid") != session.uid:
        msg_meta = None

    rag_hits, rag_ms = await vector_kb.search(raw_query, character=character, top_k=2, allow_cloud_embedding=False)
    timeline_hint = build_multi_speaker_alignment_hint(group_id, msg_meta)

    allow_stickers = bool(
        allow_stickers
        and character.strip().lower() in {"amiya", "assistant", "none", "default", ""}
        and not voice_mode_only
        and not small_talk.is_complex_or_long_query(raw_query)
    )
    sys_prompt = _build_fast_system_prompt(
        character,
        group_id,
        rag_hits,
        timeline_hint,
        user_text=raw_query,
        allow_stickers=allow_stickers,
        trigger_background_recall=not voice_mode_only,
    )
    if getattr(state, "chat_1v1_enabled", True):
        msgs = conversation.messages(sys_prompt, user_message)
        if len(msgs) > 10:
            msgs = [msgs[0], *msgs[-9:]]
    else:
        msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_message}]

    tasks: list[asyncio.Task] = []
    ds_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    ds_base = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    ds_model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-flash").strip()

    max_tok = 60 if (state.small_talk_mode or voice_mode_only) else 110

    if ds_key:
        async def _call_deepseek_with_optional_search() -> tuple[str, str]:
            from . import llm_service

            text = await llm_service._post_chat_completions(
                api_key=ds_key,
                base_url=ds_base,
                model=ds_model,
                system="",
                user="",
                max_tokens=max_tok,
                enable_web_search=True,
                messages_override=msgs,
            )
            return text, ds_model

        tasks.append(asyncio.create_task(_call_deepseek_with_optional_search()))

    if not tasks:
        return None

    winner_reply = ""
    winner_provider = ""
    try:
        # 不增加“搜索意图分析”模型调用；Flash 首轮直接决定是否搜索，留出一次搜索往返时间。
        done, pending = await asyncio.wait(tasks, timeout=24.0, return_when=asyncio.FIRST_COMPLETED)
        for d in done:
            try:
                txt, prov = d.result()
                if txt:
                    winner_reply = txt
                    winner_provider = prov
                    break
            except Exception:
                continue
        if not winner_reply and pending:
            done2, pending2 = await asyncio.wait(pending, timeout=2.2, return_when=asyncio.FIRST_COMPLETED)
            for d in done2:
                try:
                    txt, prov = d.result()
                    if txt:
                        winner_reply = txt
                        winner_provider = prov
                        break
                except Exception:
                    continue
            for p in pending2:
                p.cancel()
        else:
            for p in pending:
                p.cancel()
    except Exception:
        for t in tasks:
            t.cancel()
        return None

    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    if not winner_reply:
        return None

    from .sticker_service import parse_model_reply

    winner_reply = parse_model_reply(winner_reply, allow_sticker=allow_stickers)

    active_st = conversation.get_active_state()
    if active_st or any(k in raw_query for k in ("胶带", "嘴巴", "被封", "沙子", "背着", "俯卧撑", "取下", "放下")):
        def _creds():
            return (ds_key, ds_base, ds_model)
        winner_reply = await situational_state.resolve_and_enforce_situational_state(
            character, raw_query, winner_reply, resolve_credentials_fn=_creds, voice_mode_only=voice_mode_only,
        )

    if state.small_talk_mode and not voice_mode_only:
        st_now = conversation.get_active_state()
        winner_reply = small_talk.format_small_talk_output(
            winner_reply,
            character=character,
            speech_blocked=bool(st_now and st_now.get("speech_blocked")),
            user_text=raw_query,
        )
    else:
        winner_reply = small_talk.normalize_qq_linebreaks(winner_reply)

    total_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
    _LATENCY_METRICS.append({
        "time": datetime.datetime.now().strftime("%H:%M:%S"),
        "provider": winner_provider,
        "rag_ms": rag_ms,
        "total_ms": total_ms,
        "query": raw_query[:24],
        "reply": winner_reply[:32],
    })
    logger.info(f"[⚡极速引擎] {winner_provider} 响应完成: total={total_ms}ms (向量检索={rag_ms}ms) | 回复='{winner_reply[:35]}'")
    return winner_reply


# ---------------------------------------------------------------------------
# 每日 09:00 自动发 QQ 空间开工动态 + 白名单群早安塔罗/宜忌
# 遵循用户要求：发空间与每日问候不走 1s 秒回捷径，完整走【角色提示词 + Agent (百炼/DeepSeek) + 完整生图】链路！
# 严格自测：若生图未通过魔数校验则静默降级为纯文字，绝不图文分开发、绝不输出错误报告！
# ---------------------------------------------------------------------------

def _calc_qzone_gtk(skey: str) -> int:
    h = 5381
    for ch in skey:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


def _load_schedule_state() -> dict[str, Any]:
    if SCHEDULE_STATE_PATH.is_file():
        try:
            data = json.loads(SCHEDULE_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {
        "last_qzone_date": "",
        "last_morning_date": "",
        "last_qzone_text": "",
        "last_morning_text": "",
        "last_qzone_status": "",
        "last_qzone_has_image": False,
    }


def _save_schedule_state(data: dict[str, Any]) -> None:
    SCHEDULE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=SCHEDULE_STATE_PATH.parent, prefix=".daily-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        Path(name).replace(SCHEDULE_STATE_PATH)
    finally:
        Path(name).unlink(missing_ok=True)


async def _call_full_agent_with_preset(character: str, task_prompt: str) -> Optional[str]:
    from . import llm_service
    char = character if character in {"amiya", "kaltsit", "dania"} else "amiya"
    # 每日公开创作与触发请求的私聊上下文隔离。
    with conversation.use_session(None), conversation.history_enabled(False):
        return await asyncio.wait_for(llm_service.chat_operator(char, task_prompt, max_tokens=220), timeout=55.0)


async def _try_generate_validated_image(
    prompt: str,
    *,
    use_amiya_ref: bool = True,
    timeout_sec: float = DAILY_IMAGE_TIMEOUT_SEC,
    image_model: Optional[str] = None,
) -> Optional[bytes]:
    """走完整的提示词润色 + 角色参考图 + 生图 Agent 管线 (generate_image_with_qwen_or_wan)，并执行严格的二进制魔数自测；
    任何超时、风控或校验失败均静默返回 None，绝不抛错或输出错误报告。
    """
    try:
        from . import llm_service
        ensure_bot_env()
        api_key = (os.getenv("DASHSCOPE_API_KEY") or "").strip()
        if not api_key:
            return None
        refs: list[str] = []
        if use_amiya_ref:
            refs = llm_service._load_local_character_refs("阿米娅", max_refs=1)
            if not refs:
                logger.warning("每日阿米娅配图缺少本地角色参考图，跳过生图以免生成错误角色")
                return None
        async with asyncio.timeout(timeout_sec):
            with conversation.history_enabled(False):
                img_url = await llm_service.generate_image_with_qwen_or_wan(
                    api_key=api_key,
                    user_prompt=prompt,
                    image_b64_list=refs if refs else None,
                    image_model=image_model,
                    character_reference_only=bool(refs),
                    generation_timeout_sec=timeout_sec,
                )
            clean_url = str(img_url or "").strip()
            if clean_url.startswith("base64://"):
                raw_b = base64.b64decode(clean_url[len("base64://"):])
                return raw_b if _is_valid_image_bytes(raw_b) else None
            if ";base64," in clean_url:
                raw_b = base64.b64decode(clean_url.split(";base64,", 1)[1])
                return raw_b if _is_valid_image_bytes(raw_b) else None
            m_url = re.search(r"https?://[^\s\)\]\"'>]+", clean_url)
            if m_url:
                clean_url = m_url.group(0)
            if not clean_url.startswith(("http://", "https://")):
                return None
            client = _get_warm_client()
            resp = await client.get(clean_url, timeout=90.0, follow_redirects=True)
            if resp.status_code == 200 and _is_valid_image_bytes(resp.content):
                return resp.content
    except Exception as exc:
        logger.debug(f"配图自测未通过，自动静默降级为纯文字: {exc}")
    return None


async def generate_qzone_work_post(character: str = "amiya") -> str:
    """通过完整提示词 + Agent 生成符合当前时段的阿米娅工作空间说说文案。"""
    char = (character or "amiya").strip().lower()
    if char in ("none", "assistant", ""):
        char = "amiya"
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    now = datetime.datetime.now()
    w_name = weekday_names[now.weekday()]
    date_str = now.strftime("%m月%d日")
    period = "下午" if 12 <= now.hour < 18 else "上午"

    prompt = (
        f"今天是{date_str}（{w_name}）。请用阿米娅温柔真诚而自然的语气写一条发在QQ空间的「{period}工作动态说说」"
        f"（画面感：正坐在电脑前认真处理工作，窗外是{period}的自然光，顺便跟博士和大家打个招呼）。"
        "要求：控制在 22～45 字以内，纯文字一句说完，严禁任何括号动作或错误提示，带1个可爱的Emoji即可。"
    )
    reply = await _call_full_agent_with_preset(char, prompt)
    if reply:
        clean = re.sub(r"[（(【\[][^（）()【】\[\]]*[）)】\]]", "", reply).replace("\n", " ").strip().strip('"“”')
        if 8 <= len(clean) <= 75 and not any(bad in clean.lower() for bad in ("错误", "失败", "异常", "抱歉", "error", "http")):
            return clean
    raise ValueError("空间文案未通过校验，未发布")


async def generate_morning_tarot_quote(character: str = "amiya") -> tuple[str, str]:
    """通过完整提示词 + Agent 生成每日早上 9:00 白名单群早安语录（融合塔罗牌 + 今日宜忌，控制在 30 字左右）。"""
    char = (character or state.active_character or "amiya").strip().lower()
    if char in ("none", "assistant", ""):
        char = "amiya"
    seed_day = int(datetime.datetime.now().strftime("%Y%m%d")) + random.randint(0, 99)
    card_name, good_for, bad_for = _TAROT_DECK[seed_day % len(_TAROT_DECK)]

    prompt = (
        f"请用阿米娅温柔自然的口吻写一句群聊早安问候语录，必须包含今日塔罗牌【{card_name}】以及「宜{good_for}，忌{bad_for}」。"
        "严格控制在 25～35 个汉字左右，一句说完，不换行，不带括号动作，不要输出任何多余解释！"
    )
    reply = await _call_full_agent_with_preset(char, prompt)
    if reply:
        clean = re.sub(r"[（(][^（）()]*[）)]", "", reply).replace("\n", " ").strip().strip('"“”')
        if 16 <= len(clean) <= 48 and not any(bad in clean.lower() for bad in ("错误", "失败", "异常", "error", "http")):
            return clean, card_name
    return f"早安呀~今日塔罗是【{card_name}】，宜{good_for}，忌{bad_for}，祝今天顺顺利利✨", card_name


async def publish_qzone_post(content: str = "", *, with_image: bool = True) -> dict[str, Any]:
    """发布阿米娅开工 QQ 空间动态（走完整提示词 + Agent + 生图管线）：
    - 若 with_image=True，合成「阿米娅坐在电脑前认真工作」配图并做魔数自测；
    - 图文必须合并在同一次空间说说请求中发出（绝不图文分开发）；
    - 若配图自测未过或超时，静默退回纯文字发布，绝不输出任何错误报告！
    """
    from nonebot import get_bots
    if not (content or "").strip() and not get_bots():
        return {"code": 1, "status": "not_delivered", "status_label": "机器人未连接，未生成或发布空间内容", "published_online": False}
    text = (content or "").strip() or await generate_qzone_work_post("amiya")
    img_bytes: Optional[bytes] = None
    if with_image:
        period = "下午" if 12 <= datetime.datetime.now().hour < 18 else "上午"
        lighting = "温暖柔和的午后阳光" if period == "下午" else "清爽明亮的上午自然光"
        img_prompt = (
            "明日方舟阿米娅（棕色兔耳少女，蓝色眼睛，佩戴黑色颈环），正坐在现代温馨的办公桌电脑前认真工作，"
            f"双手轻放在键盘上，电脑屏幕散发柔和微光，{lighting}透过窗户照在桌面上，二次元高质量插画，细节精致，{period}工作氛围"
        )
        img_bytes = await _try_generate_validated_image(
            img_prompt,
            use_amiya_ref=True,
            timeout_sec=DAILY_IMAGE_TIMEOUT_SEC,
            image_model=DAILY_IMAGE_MODEL,
        )

    img_b64 = base64.b64encode(img_bytes).decode("ascii") if img_bytes else ""
    bots = get_bots()
    bot = next(iter(bots.values()), None) if bots else None
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    method_used = "local_archive"
    published_online = False

    if bot is not None:
            try:
                ck_resp = await asyncio.wait_for(bot.call_api("get_cookies", domain="qzone.qq.com"), timeout=5.0)
                cookie_str = str((ck_resp or {}).get("cookies") or "")
                p_skey = ""
                skey = ""
                for part in cookie_str.split(";"):
                    kv = part.strip().split("=", 1)
                    if len(kv) == 2:
                        if kv[0].strip() == "p_skey":
                            p_skey = kv[1].strip()
                        elif kv[0].strip() == "skey":
                            skey = kv[1].strip()
                active_key = p_skey or skey
                bot_qq = str(getattr(bot, "self_id", "") or "1412773476")
                if active_key and bot_qq:
                    g_tk = _calc_qzone_gtk(active_key)
                    headers = {
                        "Cookie": cookie_str,
                        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                        "Referer": f"https://user.qzone.qq.com/{bot_qq}",
                        "Origin": "https://user.qzone.qq.com",
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    }
                    client = _get_warm_client()
                    richval = ""
                    if img_b64:
                        try:
                            up_url = f"https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk={g_tk}"
                            up_form = urllib.parse.urlencode({
                                "filename": "filename",
                                "uin": bot_qq,
                                "skey": skey,
                                "zzpaneluin": bot_qq,
                                "p_uin": bot_qq,
                                "p_skey": p_skey,
                                "uploadtype": "1",
                                "albumtype": "7",
                                "exttype": "0",
                                "refer": "shuoshuo",
                                "output_type": "json",
                                "charset": "utf-8",
                                "output_charset": "utf-8",
                                "upload_hd": "1",
                                "hd_width": "2048",
                                "hd_height": "10000",
                                "hd_quality": "96",
                                "url": f"https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk={g_tk}",
                                "base64": "1",
                                "picfile": img_b64,
                            })
                            up_r = await client.post(up_url, content=up_form, headers=headers, timeout=20.0)
                            m_json = re.search(r"\{.*\}", up_r.text, re.DOTALL)
                            if m_json:
                                up_data = json.loads(m_json.group(0)).get("data") or {}
                                lloc = up_data.get("lloc") or ""
                                sloc = up_data.get("sloc") or lloc
                                albumid = up_data.get("albumid") or ""
                                w = up_data.get("width") or 1024
                                h = up_data.get("height") or 1024
                                if albumid and lloc:
                                    richval = f",{albumid},{lloc},{sloc},1,{h},{w},,{w},{h}"
                        except Exception:
                            richval = ""

                    qz_url = f"https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6?g_tk={g_tk}"
                    form_dict = {
                        "syn_tweet_verson": "1",
                        "paramstr": "1",
                        "pic_template": "",
                        "richtype": "1" if richval else "",
                        "richval": richval,
                        "special_url": "",
                        "subrichtype": "1" if richval else "",
                        "who": "1",
                        "con": text,
                        "feedversion": "1",
                        "ver": "1",
                        "ugc_right": "1",
                        "to_sign": "0",
                        "hostuin": bot_qq,
                        "code_version": "1",
                        "format": "json",
                        "qzreferrer": f"https://user.qzone.qq.com/{bot_qq}",
                    }
                    try:
                        r = await client.post(qz_url, content=urllib.parse.urlencode(form_dict), headers=headers, timeout=20.0)
                    except httpx.RequestError:
                        raise DeliveryUncertain("空间发布请求断连，结果未知；请先检查空间，系统不会自动重发")
                    match = re.search(r"\{.*\}", r.text, re.DOTALL)
                    try:
                        data = json.loads(match.group(0)) if match else {}
                    except ValueError as exc:
                        raise DeliveryUncertain("空间返回无法解析，投递结果未知") from exc
                    if not isinstance(data, dict):
                        raise DeliveryUncertain("空间响应结构无法确认，投递结果未知")
                    nested = data.get("data")
                    tid = data.get("tid") or (nested.get("tid") if isinstance(nested, dict) else None)
                    if r.status_code == 200 and data.get("code", 0) in (0, "0") and tid:
                        published_online = True
                        method_used = "qzone_cgi" + ("+img" if richval else "_text")
                    elif r.status_code >= 500 or (r.status_code < 400 and data.get("code", 0) in (0, "0")):
                        raise DeliveryUncertain("空间未返回可确认的说说编号，结果未知；请检查后再操作")
            except DeliveryUncertain:
                raise
            except Exception as exc:
                logger.warning(f"空间发布未完成: {type(exc).__name__}")

    posts: list[dict[str, Any]] = []
    if QZONE_HISTORY_PATH.is_file():
        try:
            loaded = json.loads(QZONE_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                posts = loaded
        except Exception:
            pass
    entry = {
        "time": now_str,
        "content": text,
        "generated_has_image": bool(img_bytes),
        "has_image": published_online and method_used.endswith("+img"),
        "published_online": published_online,
        "method": method_used,
    }
    posts.insert(0, entry)
    try:
        QZONE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        QZONE_HISTORY_PATH.write_text(json.dumps(posts[:50], ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        entry["persistence_warning"] = "空间生成/投递结果未能写入本地归档"
        logger.error(f"空间归档写入失败: {type(exc).__name__}")

    st_data = _load_schedule_state()
    if published_online:
        st_data["last_qzone_date"] = _daily_now().date().isoformat()
    st_data["last_qzone_text"] = text
    st_data["last_qzone_has_image"] = entry["has_image"]
    st_data["last_qzone_status"] = "已发布（图文一体）" if entry["has_image"] else ("已发布（纯文字）" if published_online else ("已生成图文动态" if img_bytes else "已生成纯文字动态"))
    try:
        _save_schedule_state(st_data)
    except OSError as exc:
        entry["persistence_warning"] = "投递结果已返回，本地状态保存失败"
        logger.error(f"空间状态写入失败: {type(exc).__name__}")
    logger.info(f"[🌟阿米娅开工空间] 状态={st_data['last_qzone_status']} | 文案='{text}'")
    return {"code": 0 if published_online else 1, "status": "published" if published_online else "not_delivered", **entry, "status_label": st_data["last_qzone_status"]}


async def broadcast_morning_tarot_to_groups(custom_text: str = "", *, with_image: bool = True) -> dict[str, Any]:
    from nonebot import get_bots
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    bot = next(iter(get_bots().values()), None)
    groups = list(dict.fromkeys(str(g) for g in state.active_groups if str(g).isdigit()))
    if bot is None or not groups:
        return {"code": 1, "status": "not_delivered", "status_label": "机器人未连接或未配置目标群", "sent_groups": [], "failed_groups": groups, "unknown_groups": []}
    quote, card_name = (custom_text.strip(), "今日塔罗") if custom_text.strip() else await generate_morning_tarot_quote("amiya")
    img_bytes = None
    if with_image:
        img_bytes = await _try_generate_validated_image(
            f"明日方舟阿米娅手持精美塔罗牌「{card_name}」，清晨温暖光影，二次元高质量插画",
            use_amiya_ref=True,
            timeout_sec=DAILY_IMAGE_TIMEOUT_SEC,
            image_model=DAILY_IMAGE_MODEL,
        )
    message = Message([MessageSegment.text(quote), MessageSegment.image("base64://" + base64.b64encode(img_bytes).decode())]) if img_bytes else quote
    sent, failed, unknown = [], [], []
    for gid in groups:
        try:
            receipt = await asyncio.wait_for(bot.call_api("send_group_msg", group_id=int(gid), message=message), timeout=12.0)
            if isinstance(receipt, dict) and receipt.get("message_id"):
                sent.append(gid)
            else:
                unknown.append(gid)
        except ActionFailed:
            failed.append(gid)
        except (TimeoutError, httpx.RequestError, NetworkError):
            unknown.append(gid)
        except Exception as exc:
            logger.warning(f"群 {gid} 的投递未获确认: {type(exc).__name__}")
            unknown.append(gid)
        await asyncio.sleep(0.8)
    status = "published" if len(sent) == len(groups) else ("partial" if sent else ("unknown" if unknown else "not_delivered"))
    result = {"code": 0 if status == "published" else 1, "status": status, "status_label": f"已确认送达 {len(sent)}/{len(groups)} 群，失败 {len(failed)}，未知 {len(unknown)}", "quote": quote, "length": len(quote), "has_image": bool(img_bytes), "sent_groups": sent, "failed_groups": failed, "unknown_groups": unknown}
    st_data = _load_schedule_state()
    if status == "published":
        st_data["last_morning_date"] = _daily_now().date().isoformat()
    st_data["last_morning_text"] = quote
    st_data["last_morning_status"] = result["status_label"]
    try:
        _save_schedule_state(st_data)
    except OSError as exc:
        result["persistence_warning"] = "投递结果已返回，本地状态保存失败"
        logger.error(f"早安状态写入失败: {type(exc).__name__}")
    return result


class DeliveryUncertain(RuntimeError):
    pass


def _daily_now() -> datetime.datetime:
    return datetime.datetime.now(_DAILY_TIMEZONE)


def _save_daily_job(kind: str, job: dict[str, Any]) -> None:
    data = _load_schedule_state()
    data[f"{kind}_job"] = dict(job)
    requests = data.setdefault("request_results", {})
    requests[f"{kind}:{job['request_id']}"] = dict(job)
    while len(requests) > 50:
        requests.pop(next(iter(requests)))
    _save_schedule_state(data)


def _daily_job_status(kind: str, saved: dict[str, Any]) -> dict[str, Any]:
    job = dict(_DAILY_JOBS.get(kind, saved.get(f"{kind}_job", {})))
    active = _DAILY_TASKS.get(kind)
    running = active is not None and not active.done()
    if job.get("status") in {"pending", "running"} and not running:
        job.update(code=1, status="unknown", status_label="上次任务未获最终确认，请先检查 QQ/空间")
    unknown_today = job.get("date") == _daily_now().date().isoformat() and (
        job.get("status") == "unknown" or bool(job.get("unknown_groups"))
    )
    job["can_start"] = not (running or unknown_today)
    return job


def start_daily_action(kind: str, *, content: str = "", with_image: bool = True, request_id: str = "") -> dict[str, Any]:
    if kind not in {"qzone", "morning"}:
        raise ValueError("未知每日任务")
    saved_data = _load_schedule_state()
    saved = saved_data.get(f"{kind}_job", {})
    memory_job = _DAILY_JOBS.get(kind, {})
    active = _DAILY_TASKS.get(kind)
    if active is not None and not active.done():
        return dict(_DAILY_JOBS[kind])
    if request_id and memory_job.get("request_id") == request_id:
        return dict(memory_job)
    if memory_job.get("task_id") == saved.get("task_id") and memory_job.get("status") not in {None, "pending", "running"}:
        saved = memory_job
    today = _daily_now().date().isoformat()
    request_key = f"{kind}:{request_id}"
    prior_request = saved_data.get("request_results", {}).get(request_key) if request_id else None
    if prior_request:
        if prior_request.get("status") in {"pending", "running"}:
            return {**prior_request, "code": 1, "status": "unknown", "status_label": "上次任务未获最终确认，请先检查 QQ/空间"}
        return dict(prior_request)
    if saved.get("date") == today and (saved.get("status") in {"pending", "running", "unknown"} or saved.get("unknown_groups")):
        return {**saved, "status": "unknown", "code": 1, "status_label": "上次任务结果未确认，请先检查 QQ/空间；今天不自动重发"}
    job = {"code": 0, "status": "pending", "task_id": uuid4().hex, "request_id": request_id or uuid4().hex, "date": today, "status_label": "已受理，等待生成与投递"}
    _DAILY_JOBS[kind] = job
    _save_daily_job(kind, job)
    from .history_recorder import progress_tracker
    progress_tracker.update(job["task_id"], kind="daily", title="空间发布" if kind == "qzone" else "早安群发", status="pending", stage=job["status_label"])
    _DAILY_TASKS[kind] = asyncio.create_task(_run_daily_action(kind, content, with_image))
    return dict(job)


async def _run_daily_action(kind: str, content: str, with_image: bool) -> None:
    from .history_recorder import progress_tracker, task_scope
    job = _DAILY_JOBS[kind]
    job.update(status="running", status_label="正在生成并投递")
    try:
        with task_scope(job["task_id"]), conversation.use_session(None), conversation.history_enabled(False):
            progress_tracker.update(job["task_id"], status="running", stage=job["status_label"], percent=10)
            async with asyncio.timeout(300.0):
                result = await (publish_qzone_post(content, with_image=with_image) if kind == "qzone" else broadcast_morning_tarot_to_groups(content, with_image=with_image))
            job.update(result)
    except asyncio.CancelledError:
        job.update(code=1, status="unknown", status_label="任务被取消，投递结果未确认；请检查 QQ/空间")
        raise
    except (TimeoutError, DeliveryUncertain) as exc:
        job.update(code=1, status="unknown", status_label=str(exc) or "任务超时，投递结果未知；不自动重发")
    except Exception as exc:
        logger.warning(f"每日任务 {kind} 失败: {type(exc).__name__}")
        job.update(code=1, status="error", status_label="任务未完成，请检查服务日志")
    finally:
        try:
            _save_daily_job(kind, job)
        except OSError as exc:
            job["persistence_warning"] = "本地状态保存失败，请依据 QQ/空间实际记录判断投递"
            logger.error(f"每日任务状态写入失败: {type(exc).__name__}")
        progress_tracker.update(
            job["task_id"], status="done" if job.get("code") == 0 else "error",
            stage=job["status_label"], percent=100,
        )


def _scheduler_tick(now: datetime.datetime) -> None:
    if not (getattr(state, "global_active", True) and state.daily_schedule_armed and state.daily_9am_enabled):
        return
    hour, minute = (int(part) for part in state.daily_schedule_time.split(":"))
    scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delay = (now - scheduled_at).total_seconds()
    if delay < 0 or delay > DAILY_SCHEDULE_CATCHUP_SECONDS:
        return
    today = now.date().isoformat()
    saved = _load_schedule_state()
    for kind, enabled in (("qzone", state.daily_qzone_enabled), ("morning", state.daily_morning_group_enabled)):
        if enabled and saved.get(f"{kind}_job", {}).get("date") != today and saved.get(f"last_{kind}_date") != today:
            start_daily_action(kind, request_id=f"schedule:{kind}:{today}")


async def _daily_9am_scheduler_loop() -> None:
    while True:
        await asyncio.sleep(20.0)
        try:
            _scheduler_tick(_daily_now())
        except Exception as exc:
            logger.warning(f"每日调度检查失败: {type(exc).__name__}")


def ensure_daily_scheduler_started() -> None:
    global _DAILY_SCHEDULER_TASK
    if _DAILY_SCHEDULER_TASK is None or _DAILY_SCHEDULER_TASK.done():
        _DAILY_SCHEDULER_TASK = asyncio.create_task(_daily_9am_scheduler_loop())


async def startup() -> None:
    global _RAG_INITIALIZATION_ERROR
    try:
        vector_kb.ensure_initialized()
        _RAG_INITIALIZATION_ERROR = ""
    except Exception as exc:
        _RAG_INITIALIZATION_ERROR = "知识库初始化失败，快速请求失败时转回现有模型"
        logger.error(f"知识库初始化失败: {type(exc).__name__}: {exc}")
    ensure_daily_scheduler_started()


async def shutdown() -> None:
    global _DAILY_SCHEDULER_TASK, _WARM_HTTP_CLIENT
    tasks = [task for task in [_DAILY_SCHEDULER_TASK, *_DAILY_TASKS.values()] if task and not task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    _DAILY_SCHEDULER_TASK = None
    if _WARM_HTTP_CLIENT is not None:
        await _WARM_HTTP_CLIENT.aclose()
        _WARM_HTTP_CLIENT = None


def get_fast_engine_summary() -> dict[str, Any]:
    recent = list(_LATENCY_METRICS)
    avg_ms = round(sum(x["total_ms"] for x in recent) / len(recent), 1) if recent else 0.0
    last_ms = recent[-1]["total_ms"] if recent else 0.0
    sched = _load_schedule_state()
    return {
        "accept_poke": getattr(state, "accept_poke", True),
        "fast_reply_mode": getattr(state, "fast_reply_mode", True),
        "initialization_error": _RAG_INITIALIZATION_ERROR,
        "vector_engine_mode": getattr(state, "vector_engine_mode", "hybrid"),
        "group_quote_reply": False,
        "natural_persona_mode": getattr(state, "natural_persona_mode", True),
        "chat_1v1_enabled": getattr(state, "chat_1v1_enabled", True),
        "group_buffer_enabled": getattr(state, "group_buffer_enabled", True),
        "parallel_sync_async_enabled": False,
        "conversation_policy": "同一QQ串行，不同QQ独立；历史按QQ保存",
        "daily_jobs": {kind: _daily_job_status(kind, sched) for kind in ("qzone", "morning")},
        "group_buffer_window_ms": getattr(state, "group_buffer_window_ms", 550),
        "daily_9am_enabled": bool(state.daily_9am_enabled and state.daily_schedule_armed),
        "daily_qzone_enabled": getattr(state, "daily_qzone_enabled", True),
        "daily_morning_group_enabled": getattr(state, "daily_morning_group_enabled", True),
        "daily_schedule_time": getattr(state, "daily_schedule_time", "09:00"),
        "schedule_state": sched,
        "kb_count": len(vector_kb._cache),
        "avg_latency_ms": avg_ms,
        "last_latency_ms": last_ms,
        "recent_metrics": recent[-8:],
    }


class RagConfigReq(BaseModel):
    accept_poke: bool | None = None
    fast_reply_mode: bool | None = None
    chat_1v1_enabled: bool | None = None
    group_buffer_enabled: bool | None = None
    group_buffer_window_ms: int | None = Field(default=None, ge=100, le=3000)
    parallel_sync_async_enabled: bool | None = None
    daily_9am_enabled: bool | None = None
    daily_qzone_enabled: bool | None = None
    daily_morning_group_enabled: bool | None = None
    daily_schedule_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    vector_engine_mode: Literal["local", "hybrid", "dashscope"] | None = None


class DailyResolveReq(BaseModel):
    task_id: str = Field(min_length=1)


class DailyActionReq(BaseModel):
    content: str = Field(default="", max_length=2000)
    quote: str = Field(default="", max_length=2000)
    with_image: bool = True
    request_id: str = Field(default="", max_length=100)


def register_routes(app: FastAPI) -> None:
    existing_paths = {getattr(r, "path", "") for r in app.routes}
    if "/api/rag/status" in existing_paths:
        return

    @app.get("/api/rag/status")
    async def api_rag_status(character: str = "") -> dict[str, Any]:
        return {
            "code": 0,
            "summary": get_fast_engine_summary(),
            "entries": vector_kb.list_entries(character=character),
        }

    @app.post("/api/rag/config")
    async def api_rag_config(req: RagConfigReq) -> dict[str, Any]:
        values = req.model_dump(exclude_none=True)
        if "daily_9am_enabled" in values:
            state.daily_schedule_armed = True
        # 并行输出尚无安全的历史合并协议，保留兼容字段但不把它报告为已实现能力。
        if "parallel_sync_async_enabled" in values:
            values["parallel_sync_async_enabled"] = False
        for key, value in values.items():
            setattr(state, key, value)
        await state.save()
        return {"code": 0, "summary": get_fast_engine_summary()}

    @app.post("/api/rag/add")
    async def api_rag_add(request: Request) -> dict[str, Any]:
        body = await request.json()
        item = await vector_kb.add_entry(
            character=str(body.get("character") or "amiya"),
            title=str(body.get("title") or "自定义知识"),
            content=str(body.get("content") or ""),
            keywords=str(body.get("keywords") or ""),
            category=str(body.get("category") or "custom"),
        )
        return {"code": 0, "item": item, "summary": get_fast_engine_summary()}

    @app.post("/api/rag/delete")
    async def api_rag_delete(request: Request) -> dict[str, Any]:
        body = await request.json()
        ok = vector_kb.delete_entry(int(body.get("id") or 0))
        return {"code": 0 if ok else 1, "deleted": ok, "summary": get_fast_engine_summary()}

    @app.post("/api/rag/search")
    async def api_rag_search(request: Request) -> dict[str, Any]:
        body = await request.json()
        hits, ms = await vector_kb.search(
            str(body.get("query") or ""),
            character=str(body.get("character") or state.active_character or "amiya"),
            top_k=int(body.get("top_k") or 3),
            allow_cloud_embedding=True,
        )
        return {"code": 0, "elapsed_ms": ms, "hits": hits}

    @app.post("/api/rag/benchmark")
    async def api_rag_benchmark() -> dict[str, Any]:
        t0 = time.perf_counter()
        char = state.active_character if state.active_character not in ("none", "") else "amiya"
        test_q = "博士今天有点累，阿米娅会怎么回应？"
        _, rag_ms = await vector_kb.search(test_q, character=char, top_k=2)
        reply = await fast_chat_reply(char, test_q, group_id=None, voice_mode_only=False)
        total_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        if reply:
            return {"code": 0, "rag_ms": rag_ms, "total_ms": total_ms, "reply": reply}
        return {"code": 1, "rag_ms": rag_ms, "total_ms": total_ms, "message": "未返回测试结果"}

    @app.post("/api/daily/{kind}/acknowledge")
    async def acknowledge_daily_result(kind: Literal["qzone", "morning"], req: DailyResolveReq) -> dict[str, Any]:
        saved = _load_schedule_state()
        job = _daily_job_status(kind, saved)
        if job.get("task_id") != req.task_id or job.get("can_start") is not False:
            raise HTTPException(status_code=409, detail="任务已变化，请先刷新状态")
        active = _DAILY_TASKS.get(kind)
        if active is not None and not active.done():
            raise HTTPException(status_code=409, detail="任务仍在执行，不能解除限制")
        job.update(status="acknowledged", code=1, can_start=True, status_label="用户已核实上次结果，可手动重新发起；本次没有发送消息")
        job["previous_unknown_groups"] = job.pop("unknown_groups", [])
        _save_daily_job(kind, job)
        _DAILY_JOBS[kind] = job
        return {"code": 0, "job": job}

    @app.post("/api/daily/qzone_now")
    async def api_daily_qzone_now(req: DailyActionReq) -> dict[str, Any]:
        return start_daily_action("qzone", content=req.content, with_image=req.with_image, request_id=req.request_id)

    @app.post("/api/daily/morning_now")
    async def api_daily_morning_now(req: DailyActionReq) -> dict[str, Any]:
        return start_daily_action("morning", content=req.quote or req.content, with_image=req.with_image, request_id=req.request_id)


_POKE_FALLBACK_POOL = (
    "唔…耳朵不能乱捏的啦",
    "别戳啦，兔耳会痒的",
    "呀！突然碰耳朵会吓一跳",
    "再戳的话兔耳朵要垂下来啦",
    "唔，衣角要被你拽皱啦",
)


async def generate_poke_reaction(character: str = "amiya", sender_name: str = "") -> str:
    """结合阿米娅敏感兔耳与少女特征，实时调用极速模型生成 12 字以内的鲜活被戳反应（绝不机械喊博士）。"""
    ensure_bot_env()
    char = (character or "amiya").strip().lower()
    if char in ("none", "default", ""):
        char = "amiya"

    sys_map = {
        "amiya": (
            "你是卡特斯（兔子）少女阿米娅，头顶有一对神经非常敏感的长兔耳。"
            "刚刚群友轻轻戳了戳你的脸颊或偷摸了一下你的兔耳朵。请实时回一句自然鲜活、带点害羞或小声抗议的口语反应！\n"
            "输出铁律：\n"
            "1. 严格控制在 4～12 个汉字以内，一句话说完！\n"
            "2. 严禁出现「博士」二字！\n"
            "3. 严禁使用括号动作描写（如（害羞）），严禁句末加句号。"
        ),
        "dania": "你是傲娇少女达妮娅。有人刚刚戳了戳你，请回一句 12 字以内的傲娇小抗议，不带括号和句号。",
        "kaltsit": "你是冷静的凯尔希。有人刚刚戳了戳你，请回一句 12 字以内的简短提醒，不带括号和句号。",
    }
    sys_prompt = sys_map.get(char, sys_map["amiya"])
    user_prompt = f"{sender_name or '有人'}刚刚戳了戳你的兔耳朵，请立刻做出反应（12字以内）："

    ds_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    ds_base = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    ds_model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-flash").strip()
    msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}]

    tasks: list[asyncio.Task] = []
    if ds_key:
        tasks.append(asyncio.create_task(
            _call_single_fast_endpoint(
                f"{ds_base}/chat/completions",
                ds_key,
                {
                    "model": ds_model,
                    "messages": msgs,
                    "max_tokens": 28,
                    "temperature": 0.9,
                    "thinking": {"type": "disabled"},
                },
                ds_model,
            )
        ))

    reply_txt = ""
    if tasks:
        try:
            done, pending = await asyncio.wait(tasks, timeout=2.2, return_when=asyncio.FIRST_COMPLETED)
            for d in done:
                try:
                    txt, _ = d.result()
                    if txt:
                        reply_txt = txt
                        break
                except Exception:
                    continue
            for p in pending:
                p.cancel()
        except Exception:
            for t in tasks:
                t.cancel()
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)

    if not reply_txt:
        reply_txt = random.choice(_POKE_FALLBACK_POOL)

    return small_talk.format_small_talk_output(reply_txt, character=char, is_question=False)


async def broadcast_morning_greeting(*, with_image: bool = True) -> dict[str, Any]:
    """兼容早安塔罗同气泡广播入口。"""
    return await broadcast_morning_tarot_to_groups(with_image=with_image)
