#!/usr/bin/env python3
"""一键升级与修复脚本：
1. 扫描 bot/data/refs/ 下的所有立绘文件，自动用 macOS 原生 sips 归一化为 <=1536px 的标准 RGB JPEG 缓存 (bot/data/cache/refs_norm/)，
   彻底解决网上下载的透明大尺寸 PNG / WebP 伪装图导致百炼 qwen-image-2.1-pro 报 HTTP 400 InvalidParameter 或上传超时断连的问题！
2. 修正单字/短名误匹配（如“今天/今年”误中“年.png”、“/draw”误中“w.png”、“菲比啾比”同时命中两张立绘）。
3. 制定单人 vs 多人同框规则：
   - 单角色生图：挂载 1 张归一化后的本地立绘 (I2I) + 视觉锚点描述；
   - 多角色同框生图：自动汇总各角色的文字视觉锚点（预设 + refs_visual_cache.json），走纯文生图 (T2I) + 左右分离构图，避免多张白底立绘垫图超时或特征缝合。
4. 增加 I2I -> T2I 自动兜底：若带参考图调用生图接口因网关波动或图片受限失败，自动剥离参考图、保留完整外貌 Prompt 重试一次纯文生图，杜绝动辄报错“画布渲染连接出现网络波动”。
5. 生成本地角色别称库 (bot/data/knowledge/char_aliases.json) 与阿里百炼 RAG 知识库文档 (bot/data/knowledge/char_aliases_rag.md)。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

WORKSPACE = Path.home() / "Desktop" / "qq-agent-workspace"
BOT_ROOT = WORKSPACE / "bot"
KNOWLEDGE_DIR = BOT_ROOT / "data" / "knowledge"
REFS_DIR = BOT_ROOT / "data" / "refs"
REFS_NORM_DIR = BOT_ROOT / "data" / "cache" / "refs_norm"
LLM_SERVICE_PATH = BOT_ROOT / "src" / "plugins" / "agent_core" / "llm_service.py"

CHAR_ALIASES_DATA = {
    "_meta": {
        "description": "二次元角色标准名与常用别称/外号/英文名映射表（自动对齐 bot/data/refs/ 立绘与百炼提示词 Agent 97fcb1dc96504f879c58f3b4c3a3e6d0）",
        "sources": [
            "https://github.com/CM-Edelweiss/WutheringWavesUID",
            "https://github.com/Loping151/XutheringWavesUID",
            "https://github.com/lgc-NB2Dev/bawiki-data",
            "https://github.com/nonebotjs/nonebot-plugin-arktools",
            "https://github.com/gxy12345/arknights-plugin",
        ],
    },
    "aliases": {
        # 鸣潮 (Wuthering Waves)
        "菲比啾比": ["菲比啾比", "chibi phoebe", "q版菲比", "菲比团子", "菲比表情包", "啾比", "菲比拉电线", "我坐好了"],
        "菲比": ["菲比", "phoebe", "菲比马里诺", "小修女", "隐海修女", "光噪修女", "拉古那修女"],
        "达妮娅": ["达妮娅", "dania", "星炬达妮娅", "虚质科学部少女"],
        "守岸人": ["守岸人", "shorekeeper", "岸宝", "守岸", "黑海岸神女", "蝴蝶老婆"],
        "今汐": ["今汐", "jinhsi", "今州令尹", "令尹", "汐汐", "小龙女", "白毛令尹", "汐宝"],
        "长离": ["长离", "changli", "离姐", "参事", "长离大人", "火凤", "小凤凰"],
        "椿": ["椿", "camellya", "红椿", "白椿", "山茶花", "小春"],
        "珂莱塔": ["珂莱塔", "carlotta", "二小姐", "莫塔里二小姐", "小土豆", "冰枪大小姐", "卡洛塔"],
        "洛可可": ["洛可可", "roccia", "佩洛", "大副", "愚人剧团大副"],
        "布兰特": ["布兰特", "brant", "船长", "愚人船长"],
        "坎特蕾拉": ["坎特蕾拉", "cantarella", "水母姐", "翡萨烈家主"],
        "赞妮": ["赞妮", "zani", "金库保安", "打工人赞妮", "白毛恶魔角"],
        "夏空": ["夏空", "ciaccona", "吟游诗人", "红发诗人"],
        "卡提希娅": ["卡提希娅", "cartethyia", "圣女", "芙露德莉斯", "双形态圣女", "小卡", "大卡"],
        "弗洛洛": ["弗洛洛", "phrolova", "彼岸花少女", "残星会弗洛洛"],
        "折枝": ["折枝", "zhezhi", "小画家", "画师少女", "眼镜画师"],
        "吟霖": ["吟霖", "yinlin", "雷鞭姐", "悬音"],
        "忌炎": ["忌炎", "jiyan", "忌炎将军", "夜归将军", "青龙将军"],
        "相里要": ["相里要", "xiangli yao", "要哥", "折金手"],
        "安可": ["安可", "encore", "羊咩", "粉毛萝莉", "黑白羊"],
        "维里奈": ["维里奈", "verina", "小草神", "花匠萝莉"],
        "散华": ["散华", "sanhua", "冷面保镖", "红瞳修罗"],
        "秧秧": ["秧秧", "yangyang", "看板娘秧秧", "流息少女"],
        "漂泊者": ["漂泊者", "rover", "阿漂", "男漂", "女漂"],
        # 明日方舟 (Arknights)
        "阿米娅": ["阿米娅", "amiya", "兔兔", "阿米驴", "小兔子", "罗德岛领袖", "剑兔", "奶兔", "魔王兔"],
        "凯尔希": ["凯尔希", "kaltsit", "kal'tsit", "老女人", "前妻", "凯太后", "绿猫", "老猫"],
        "维什戴尔": ["维什戴尔", "wisadel", "wiš'adel", "ew", "异格w", "蟑螂", "新议长", "大w"],
        "W": ["达布尔尤", "老w", "萨卡兹佣兵w"],
        "逻各斯": ["逻各斯", "logos", "罗神", "女妖之主", "大咒术师", "滑板鞋冠军"],
        "黍": ["黍", "shu", "黍宝", "黍姐姐", "岁家六姐", "农神", "种地龙"],
        "年": ["年泡泡", "辣脆", "岁家年"],
        "夕": ["夕宝", "小画家夕", "家里蹲龙", "岁家夕"],
        "令": ["令姐", "岁家大姐", "酒仙龙", "岁家令"],
        "重岳": ["重岳", "chongyue", "大哥", "朔", "你们解决不了"],
        "玛恩纳": ["玛恩纳", "mlynar", "叔叔", "老玛", "看报骑士", "临光叔叔"],
        "史尔特尔": ["史尔特尔", "surtr", "42", "42姐", "奶奶", "冰淇淋女王"],
        "桃金娘": ["桃金娘", "myrtle", "桃子", "大将军", "投降先锋"],
        "艾雅法拉": ["艾雅法拉", "eyjafjalla", "小羊", "火山羊", "奶羊", "纯烬艾雅法拉"],
        "能天使": ["能天使", "exusiai", "阿能", "苹果派", "过载天使"],
        "德克萨斯": ["德克萨斯", "texas", "德狗", "缄默德克萨斯", "翼德", "快活德"],
        "拉普兰德": ["拉普兰德", "lappland", "拉狗", "荒芜拉普兰德", "异格拉狗"],
        "斯卡蒂": ["斯卡蒂", "skadi", "蒂蒂", "虎鲸", "浊心斯卡蒂", "红蒂", "深海猎人"],
        "缪尔赛思": ["缪尔赛思", "muelsyse", "缪缪", "流形", "水精灵主任"],
        "妮芙": ["妮芙", "nymph", "小魅魔", "心胜于物"],
        "佩佩": ["佩佩", "pepe", "猫猫锤", "法老妹"],
        "泥岩": ["泥岩", "mudrock", "大锤姐", "转圈圈", "白发萨卡兹"],
        "铃兰": ["铃兰", "suzuran", "小狐狸", "我们的光", "九尾萝莉"],
        # 蔚蓝档案 (Blue Archive)
        "砂狼白子": ["砂狼白子", "白子", "shiroko", "抢银行", "单车狼", "黑子", "色彩白子"],
        "小鸟游星野": ["小鸟游星野", "星野", "hoshino", "粉毛大叔", "临战星野"],
        "空崎日奈": ["空崎日奈", "日奈", "hina", "委员长", "阳奈", "礼服日奈"],
        "圣园未花": ["圣园未花", "未花", "mika", "大猩猩公主", "咪卡"],
        "早濑优香": ["早濑优香", "优香", "yuuka", "100kg", "百公斤", "计算器老婆"],
        "生盐诺亚": ["生盐诺亚", "诺亚", "noa", "记事本", "白毛书记"],
        "天童爱丽丝": ["天童爱丽丝", "爱丽丝", "aris", "alice", "光之剑", "机娘女儿", "邦邦咔邦"],
        "陆八魔阿露": ["陆八魔阿露", "阿露", "aru", "吃瘪社长", "便利屋68"],
        "龙华妃咲": ["龙华妃咲", "妃咲", "kisaki", "玄龙门主", "旗袍萝莉"],
        # 社区二创看板娘
        "大肥鱼": ["大肥鱼", "蓝色大肥鱼", "deepseek娘", "deepseek看板娘", "鲸鱼娘", "蓝鲸娘", "鲸鱼女仆", "小鲸鱼", "ds娘"],
    },
}


def clean_ref_stem(stem: str) -> str:
    """剥离立绘文件名中的常见噪点词与编号（如 '立绘_守岸人_01' -> '守岸人'）。"""
    s = re.sub(r"\.(?:png|jpg|jpeg|webp|gif)$", "", stem, flags=re.IGNORECASE)
    s = re.sub(r"[（(【\[][^）)】\]]*[）)】\]]", "", s)
    s = re.sub(
        r"(?:角色图|设定图|人设图|立绘|原画|全身|半身|白底|参考图|ref|char|img|[-_\s]+\d+$|^\d+[-_\s]+)",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return s.strip(" _-.")


def pre_normalize_ref_image(src_path: Path) -> Path:
    """将 refs/ 下的原始大图（可能为 4K 透明 PNG 或伪装后缀的 WebP）用 macOS sips 归一化为 <=1536px 的标准 JPEG。"""
    REFS_NORM_DIR.mkdir(parents=True, exist_ok=True)
    out_jpg = REFS_NORM_DIR / f"{src_path.stem}.norm.jpg"
    try:
        if out_jpg.is_file() and out_jpg.stat().st_mtime >= src_path.stat().st_mtime and out_jpg.stat().st_size > 500:
            return out_jpg
        subprocess.run(
            [
                "/usr/bin/sips",
                "-Z",
                "1536",
                "-s",
                "format",
                "jpeg",
                "-s",
                "formatOptions",
                "90",
                str(src_path),
                "--out",
                str(out_jpg),
            ],
            check=True,
            capture_output=True,
            timeout=10.0,
        )
        if out_jpg.is_file() and out_jpg.stat().st_size > 500:
            return out_jpg
    except Exception as exc:
        print(f"⚠️ 归一化 {src_path.name} 失败，保留原图: {exc}")
    return src_path


ALIAS_HELPER_CODE = '''
# --- 自动角色别称解析、refs/ 图像尺寸格式归一化与智能匹配扩展 ---
_ALIAS_FILE = _KNOWLEDGE / "char_aliases.json"
_REFS_CACHE_FILE = _REFS_DIR / "refs_visual_cache.json"
_REFS_NORM_DIR = _BOT_ROOT / "data" / "cache" / "refs_norm"
# 易与日常用语冲突的单字角色名，必须显式带游戏语境或别名才匹配，严禁裸单字子串匹配（防止“今天/今年”误中“年”）
_SINGLE_CHAR_NAMES = {"年", "夕", "令", "W", "w"}


def _clean_ref_filename_stem(stem: str) -> str:
    """清洗 refs/ 下的立绘文件名噪点（如 '立绘_守岸人_01' -> '守岸人'）。"""
    s = re.sub(r"[（(【\\[\\]][^）)】\\]]*[）)】\\]]", "", stem or "")
    s = re.sub(
        r"(?:角色图|设定图|人设图|立绘|原画|全身|半身|白底|参考图|ref|char|img|[_\\-\\s]+\\d+$|^\\d+[_\\-\\s]+)",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return s.strip(" _-.")


def _normalize_ref_file_to_data_uri(p: Path) -> Optional[str]:
    """将 refs/ 下的立绘（可能是超大图/透明PNG/WebP伪装后缀）通过 macOS sips 归一化为 <=1536px 的标准 RGB JPEG data URI，彻底杜绝百炼 HTTP 400 InvalidParameter 与上传断连。"""
    import subprocess
    try:
        _REFS_NORM_DIR.mkdir(parents=True, exist_ok=True)
        norm_jpg = _REFS_NORM_DIR / f"{p.stem}.norm.jpg"
        if not norm_jpg.is_file() or norm_jpg.stat().st_mtime < p.stat().st_mtime or norm_jpg.stat().st_size <= 500:
            subprocess.run(
                [
                    "/usr/bin/sips",
                    "-Z",
                    "1536",
                    "-s",
                    "format",
                    "jpeg",
                    "-s",
                    "formatOptions",
                    "90",
                    str(p),
                    "--out",
                    str(norm_jpg),
                ],
                check=True,
                capture_output=True,
                timeout=8.0,
            )
        target_p = norm_jpg if (norm_jpg.is_file() and norm_jpg.stat().st_size > 500) else p
        raw = target_p.read_bytes()
        if len(raw) <= 100:
            return None
        # 根据真实文件头魔数判定 MIME，绝不盲信文件后缀
        if raw[:3] == b"\\xff\\xd8\\xff":
            mime = "image/jpeg"
        elif raw[:8] == b"\\x89PNG\\r\\n\\x1a\\n":
            mime = "image/png"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            mime = "image/jpeg"
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as exc:
        logger.warning(f"归一化参考立绘 {p.name} 失败: {exc}")
        return None


def _load_alias_map() -> dict[str, list[str]]:
    """读取 bot/data/knowledge/char_aliases.json 中的角色别称字典，并自动合并 refs/ 目录下的文件名。"""
    base_map: dict[str, list[str]] = {}
    try:
        if _ALIAS_FILE.is_file():
            data = json.loads(_ALIAS_FILE.read_text(encoding="utf-8"))
            raw_aliases = data.get("aliases") or {}
            if isinstance(raw_aliases, dict):
                for k, v in raw_aliases.items():
                    if isinstance(v, list):
                        base_map[str(k)] = [str(x) for x in v if x]
    except Exception as exc:
        logger.warning(f"读取角色别称表失败: {exc}")

    if _REFS_DIR.is_dir():
        for p in sorted(_REFS_DIR.iterdir()):
            if not p.is_file() or p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            cleaned = _clean_ref_filename_stem(p.stem)
            if not cleaned:
                continue
            matched_canon = None
            for canon, alist in base_map.items():
                if cleaned.lower() == canon.lower() or any(cleaned.lower() == a.lower() for a in alist):
                    matched_canon = canon
                    break
            target = matched_canon or cleaned
            cur_list = base_map.get(target, [target])
            for item in (target, p.stem, cleaned):
                if item and item not in _SINGLE_CHAR_NAMES and item not in cur_list:
                    cur_list.append(item)
            base_map[target] = cur_list
    return base_map


def _resolve_canonical_characters(user_prompt: str) -> list[str]:
    """从用户输入中精准识别所有登场角色（支持标准名、外号、别称、英文名及 refs/ 文件名），返回去重后的标准角色名列表。"""
    s = (user_prompt or "").lower()
    if not s:
        return []
    alias_map = _load_alias_map()
    matched: list[str] = []
    pairs: list[tuple[str, str]] = []
    for canon, alist in alias_map.items():
        candidates = set(alist)
        if canon not in _SINGLE_CHAR_NAMES:
            candidates.add(canon)
        for a in candidates:
            a_clean = a.strip()
            if not a_clean or a_clean in _SINGLE_CHAR_NAMES:
                continue
            if len(a_clean) >= 2 or a_clean in {"椿", "黍"}:
                pairs.append((a_clean, canon))
    pairs.sort(key=lambda x: len(x[0]), reverse=True)

    for alias_str, canon in pairs:
        alower = alias_str.lower()
        # 纯英文/数字别称（如 ew, 42, sk, ami, aru）必须满足词边界，严禁匹配到 /draw 或普通英文单词内部
        if re.fullmatch(r"[a-z0-9_\\-']+", alower):
            if not re.search(rf"(?<![a-z0-9]){re.escape(alower)}(?![a-z0-9])", s):
                continue
        elif alower not in s:
            continue
        if canon == "菲比" and "菲比啾比" in matched:
            continue
        if canon not in matched:
            matched.append(canon)
    return matched


def _normalize_prompt_with_aliases(user_prompt: str) -> str:
    """若用户使用了角色外号/别称（如‘二小姐’、‘岸宝’、‘42姐’），自动附注标准角色名。"""
    canons = _resolve_canonical_characters(user_prompt)
    if not canons:
        return user_prompt
    missing_canons = [c for c in canons if c.lower() not in (user_prompt or "").lower()]
    if not missing_canons:
        return user_prompt
    return f"{user_prompt}（注：登场角色标准名为 {', '.join(missing_canons)}）"
'''


def patch_llm_service() -> None:
    if not LLM_SERVICE_PATH.is_file():
        print(f"⚠️ 未找到 {LLM_SERVICE_PATH}，跳过代码补丁")
        return

    src = LLM_SERVICE_PATH.read_text(encoding="utf-8")

    # 若之前打过旧版补丁，先清理旧版块再注入最新版
    if "# --- 自动角色别称解析" in src:
        src = re.sub(
            r"\n# --- 自动角色别称解析.*?return f\"\{user_prompt\}（注：.*?\"\n",
            "\n",
            src,
            flags=re.DOTALL,
        )

    anchor = '_REFS_DIR = _BOT_ROOT / "data" / "refs"\n'
    if anchor in src:
        src = src.replace(anchor, anchor + ALIAS_HELPER_CODE + "\n", 1)

    old_load_func_pattern = re.compile(
        r"def _load_local_character_refs\(user_prompt: str\) -> list\[str\]:.*?(?=\n(?:def |async def |_CHARACTER_VISUAL_PRESETS))",
        re.DOTALL,
    )
    new_load_func = '''def _load_local_character_refs(user_prompt: str) -> list[str]:
    """根据用户指令精准加载 bot/data/refs/ 中的参考立绘（自动经 sips 归一化为 <=1536px 标准 JPEG，且每个角色最多取 1 张代表立绘）。"""
    if not user_prompt or not _REFS_DIR.is_dir():
        return []
    matched_canons = _resolve_canonical_characters(user_prompt)
    if not matched_canons:
        return []
    alias_map = _load_alias_map()

    all_ref_files = [
        p
        for p in sorted(_REFS_DIR.iterdir())
        if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ]
    selected_files: list[Path] = []
    # 按识别出的标准角色名逐一寻找最佳匹配的 1 张立绘（避免同一角色塞入多张冲突立绘）
    for canon in matched_canons:
        canon_lower = canon.lower()
        canon_aliases = {a.lower() for a in alias_map.get(canon, [])} | {canon_lower}
        best_file: Optional[Path] = None
        for p in all_ref_files:
            raw_stem = p.stem.lower()
            clean_stem = _clean_ref_filename_stem(p.stem).lower()
            if clean_stem == canon_lower or raw_stem == canon_lower:
                best_file = p
                break
            if clean_stem in canon_aliases or raw_stem in canon_aliases:
                best_file = p
        if best_file and best_file not in selected_files:
            selected_files.append(best_file)

    results: list[str] = []
    for p in selected_files[:2]:
        uri = _normalize_ref_file_to_data_uri(p)
        if uri:
            results.append(uri)
            logger.info(f"自动命中并归一化本地角色立绘: {p.name} (URI len={len(uri)})")
    return results
'''
    src, _ = old_load_func_pattern.subn(new_load_func + "\n\n", src, count=1)

    # 升级 _get_matched_character_keys
    old_get_keys_pattern = re.compile(
        r"def _get_matched_character_keys\(user_prompt: str\) -> list\[str\]:.*?(?=\ndef _is_multi_character_scene)",
        re.DOTALL,
    )
    new_get_keys = '''def _get_matched_character_keys(user_prompt: str) -> list[str]:
    """返回用户指令中命中的去重角色名列表（结合内置预设与 char_aliases.json 别称映射，杜绝单字误匹配）。"""
    prompt_lower = (user_prompt or "").lower()
    used_spans: list[str] = []
    for cname in sorted(_CHARACTER_VISUAL_PRESETS.keys(), key=len, reverse=True):
        if cname in _SINGLE_CHAR_NAMES:
            continue
        clower = cname.lower()
        if clower in prompt_lower:
            if cname == "菲比" and "菲比啾比" in used_spans:
                continue
            if cname == "deepseek" and "大肥鱼" in used_spans:
                continue
            used_spans.append(cname)
    for canon in _resolve_canonical_characters(user_prompt):
        if canon not in used_spans:
            if canon == "菲比" and "菲比啾比" in used_spans:
                continue
            used_spans.append(canon)
    return used_spans


'''
    src, _ = old_get_keys_pattern.subn(new_get_keys, src, count=1)

    # 升级 _build_matched_presets_hint 支持 refs_visual_cache.json
    old_build_hint_head = '    matched_entries = [\n        f"- 【独立角色 {idx + 1}（{cname}）】：{_CHARACTER_VISUAL_PRESETS[cname]}"\n        for idx, cname in enumerate(used_spans)\n    ]'
    new_build_hint_head = '''    ref_cache: dict[str, str] = {}
    try:
        if _REFS_CACHE_FILE.is_file():
            ref_cache = json.loads(_REFS_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        ref_cache = {}
    matched_entries = []
    for idx, cname in enumerate(used_spans):
        desc = _CHARACTER_VISUAL_PRESETS.get(cname) or ref_cache.get(cname) or f"二次元角色「{cname}」（请严格依据输入参考图或官方立绘设定刻画五官、发色与代表性服饰）"
        matched_entries.append(f"- 【独立角色 {idx + 1}（{cname}）】：{desc}")'''
    if old_build_hint_head in src:
        src = src.replace(old_build_hint_head, new_build_hint_head, 1)

    # 在 extract_character_visual_traits 中缓存新角色的视觉特征到 refs_visual_cache.json
    cache_save_hook = '                    logger.info(f"Qwen-VL 垫图编辑指令生成完成: {vl_res[:90]}...")'
    cache_save_replacement = '''                    logger.info(f"Qwen-VL 垫图编辑指令生成完成: {vl_res[:90]}...")
                    try:
                        canons = _resolve_canonical_characters(user_prompt)
                        if len(canons) == 1 and canons[0] not in _CHARACTER_VISUAL_PRESETS and vl_res:
                            c_map = json.loads(_REFS_CACHE_FILE.read_text(encoding="utf-8")) if _REFS_CACHE_FILE.is_file() else {}
                            if canons[0] not in c_map:
                                c_map[canons[0]] = vl_res[:260]
                                _REFS_CACHE_FILE.write_text(json.dumps(c_map, ensure_ascii=False, indent=2), encoding="utf-8")
                                logger.info(f"已自动将新角色「{canons[0]}」的视觉特征归档至 refs_visual_cache.json")
                    except Exception:
                        pass'''
    if cache_save_hook in src and "refs_visual_cache.json" not in src.split(cache_save_hook)[1][:400]:
        src = src.replace(cache_save_hook, cache_save_replacement, 1)

    # 升级 chat_operator 中的 refs 选择与 I2I -> T2I 自动降级兜底
    old_refs_block_pattern = re.compile(
        r"    if is_draw and not images and user:\n.*?images = candidate_refs",
        re.DOTALL,
    )
    new_refs_block = '''    if is_draw and not images and user:
        matched_keys = _get_matched_character_keys(user)
        # 关键规则：若为双人/多人同框场景（len >= 2），不直接传多张独立白底立绘（避免百炼多图超时断连或把两人五官衣服缝合到一人身上），
        # 而是通过 _build_matched_presets_hint + refs_visual_cache.json 提取各自文字设定交给百炼提示词 Agent 做左右分立构图！
        if _is_multi_character_scene(user) or len(matched_keys) >= 2:
            logger.info(
                f"检测到多角色同框场景 ({matched_keys})，采用双人独立文字设定锚点 + 纯文生图 (T2I) 防缝合与防超时模式"
            )
            images = []
        else:
            images = _load_local_character_refs(user)[:1]'''
    src, _ = old_refs_block_pattern.subn(new_refs_block, src, count=1)

    # 增加 I2I 失败自动回退 T2I 兜底
    old_call_gen_pattern = re.compile(
        r"    # A2\) 生图委托：直接调用 qwen-image-2\.1-pro.*?(?=\n    # B\) 百炼应用)",
        re.DOTALL,
    )
    new_call_gen_block = '''    # A2) 生图委托：直接调用 qwen-image-2.1-pro（支持参考图 I2I 及失败自动降级纯文生图 T2I 兜底）
    extra_traits = ""
    if is_draw and ds_key:
        user_norm = _normalize_prompt_with_aliases(user or "")
        if images:
            extra_traits = await extract_character_visual_traits(ds_key, images[0], user_norm)
        else:
            extra_traits = await search_character_visual_sheet(ds_key, user_norm)

        qwen_img_reply = await generate_image_with_qwen_or_wan(
            api_key=ds_key,
            user_prompt=user_norm,
            image_b64_list=images,
            character=char,
            visual_traits=extra_traits,
        )
        # 关键冗余：若带本地 refs 参考图 (I2I) 因原图尺寸/比例/网关限制失败，立即用已提取的 extra_traits 降级重试一次纯文生图 (T2I)！
        if not qwen_img_reply and images:
            logger.warning("带参考图 (I2I) 生图未返回结果，自动使用已提取的角色特征降级重试纯文生图 (T2I)...")
            if not extra_traits:
                extra_traits = await search_character_visual_sheet(ds_key, user_norm)
            qwen_img_reply = await generate_image_with_qwen_or_wan(
                api_key=ds_key,
                user_prompt=user_norm,
                image_b64_list=[],
                character=char,
                visual_traits=extra_traits,
            )
        if qwen_img_reply:
            return qwen_img_reply
        logger.warning("is_draw=True 但生图模型均未返回有效图片，拦截纯文本幻觉回退")
        return "博士，刚才画布渲染连接出现网络波动，图片没能成功传回，请再对阿米娅发一次指令试试哦~"
'''
    src, _ = old_call_gen_pattern.subn(new_call_gen_block, src, count=1)

    LLM_SERVICE_PATH.write_text(src, encoding="utf-8")
    print(f"✅ 已完成 llm_service.py 升级（包含 sips 图像归一化 + 防单字误匹配 + 多角色防超时 + I2I失败自动回退T2I）")


def main() -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    REFS_NORM_DIR.mkdir(parents=True, exist_ok=True)

    ref_files = sorted(
        [
            p
            for p in REFS_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ]
    )
    aliases_map = dict(CHAR_ALIASES_DATA["aliases"])
    discovered_refs: dict[str, str] = {}

    for p in ref_files:
        norm_p = pre_normalize_ref_image(p)
        raw_stem = p.stem
        cleaned = clean_ref_stem(raw_stem)
        if not cleaned:
            continue
        matched_canonical = None
        for canon, alist in aliases_map.items():
            if cleaned.lower() == canon.lower() or any(cleaned.lower() == a.lower() for a in alist):
                matched_canonical = canon
                break
        target_name = matched_canonical or cleaned
        existing = list(aliases_map.get(target_name, [target_name]))
        for candidate in (target_name, raw_stem, cleaned):
            if candidate and candidate not in {"年", "夕", "令", "W", "w"} and candidate not in existing:
                existing.append(candidate)
        aliases_map[target_name] = existing
        discovered_refs[target_name] = f"{p.name} (已归一化缓存: {norm_p.name}, {norm_p.stat().st_size // 1024}KB)"

    CHAR_ALIASES_DATA["aliases"] = aliases_map
    CHAR_ALIASES_DATA["discovered_ref_files"] = discovered_refs

    alias_json_path = KNOWLEDGE_DIR / "char_aliases.json"
    alias_json_path.write_text(
        json.dumps(CHAR_ALIASES_DATA, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"✅ 已生成本地角色别称与立绘映射表: {alias_json_path} (共收录 {len(aliases_map)} 名角色, 归一化处理 refs 立绘 {len(ref_files)} 张)")
    if discovered_refs:
        print("📂 已自动绑定并归一化的本地 refs 立绘:")
        for k, v in discovered_refs.items():
            print(f"   - {k} -> {v}")

    rag_lines = [
        "# 二次元角色标准名称、玩家别称外号与本地参考立绘检索表 (RAG 知识库)",
        "",
        "当用户输入外号、黑话、简称或英文名要求画图时，请先根据下表归一化为【标准角色名】，再按对应角色外观设定生成提示词：",
        "",
        "| 标准角色名 | 常见玩家别称 / 外号 / 英文名 | 本地 refs 立绘状态 |",
        "| :--- | :--- | :--- |",
    ]
    for canon, alist in aliases_map.items():
        ref_fname = discovered_refs.get(canon, "未归档本地图（走联网/预设锚点）")
        rag_lines.append(f"| **{canon}** | {', '.join(alist)} | `{ref_fname}` |")

    rag_md_path = KNOWLEDGE_DIR / "char_aliases_rag.md"
    rag_md_path.write_text("\n".join(rag_lines) + "\n", encoding="utf-8")
    print(f"✅ 已生成阿里百炼 RAG 知识库文档: {rag_md_path}")

    patch_llm_service()


if __name__ == "__main__":
    main()
