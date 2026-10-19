#!/usr/bin/env python3
"""四大二次元游戏（明日方舟、鸣潮、崩铁、原神）官方角色立绘实况下载进度监控大盘。

用法:
  python bot/scripts/monitor_ref_progress.py          # 动态全屏刷新 (默认 2 秒刷新一次)
  python bot/scripts/monitor_ref_progress.py --interval 1 # 自定义刷新间隔
  python bot/scripts/monitor_ref_progress.py --once       # 单次输出当前进度快照
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# 项目根路径
SCRIPT_DIR = Path(__file__).resolve().parent
BOT_ROOT = SCRIPT_DIR.parent
REFS_DIR = BOT_ROOT / "data" / "refs"

# ANSI 颜色定义
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[36m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_BLUE = "\033[34m"
C_MAGENTA = "\033[35m"
C_RED = "\033[31m"
C_WHITE = "\033[37m"
C_BG_BLUE = "\033[44m"
C_BG_DARK = "\033[40m"

# 四大游戏代表性角色特征库（用于自动分类与归档归属）
GAME_KEYWORDS = {
    "崩坏：星穹铁道": {
        "阿兰", "阿格莱雅", "白露", "黑天鹅", "波提欧", "布洛妮娅", "大黑塔", "大丽花",
        "丹恒", "翡翠", "符玄", "寒鸦", "黑塔", "花火", "黄泉", "霍霍", "藿藿", "姬子",
        "吉尔伽美什", "加拉赫", "椒丘", "杰帕德", "景元", "镜流", "卡芙卡", "克拉拉",
        "玲可", "流萤", "乱破", "卢卡", "罗刹", "米沙", "娜塔莎", "佩拉", "青雀", "刃",
        "三月七", "桑博", "砂金", "素裳", "停云", "托帕", "瓦尔特", "希儿", "希露瓦",
        "星期日", "雪衣", "彦卿", "银狼", "银枝", "驭空", "真理医生", "知更鸟", "云骑",
        "火花", "海瑟音", "白厄", "灵砂", "貊泽", "飞霄", "归忘之流云"
    },
    "原神": {
        "阿贝多", "阿蕾奇诺", "艾尔海森", "爱可菲", "八重神子", "白术", "班尼特", "北斗",
        "达达利亚", "迪奥娜", "迪卢克", "迪希雅", "多莉", "珐露珊", "菲谢尔", "枫原万叶",
        "芙宁娜", "甘雨", "胡桃", "基尼奇", "嘉明", "坎蒂丝", "柯莱", "可莉", "克洛琳德",
        "刻晴", "雷电将军", "雷泽", "丽莎", "林尼", "琳妮特", "流浪者", "鹿野院平藏",
        "罗莎莉亚", "玛拉妮", "玛薇卡", "米卡", "莫娜", "纳西妲", "那维莱特", "娜维娅",
        "凝光", "诺艾尔", "欧洛伦", "琴", "恰斯卡", "七七", "绮良良", "茜特菈莉",
        "赛诺", "珊瑚宫心海", "神里绫华", "神里绫人", "申鹤", "温迪", "夏洛蒂", "闲云",
        "香菱", "宵宫", "小鹿", "魈", "辛焱", "行秋", "烟绯", "夜兰", "一斗", "优菈",
        "早柚", "重云", "钟离"
    },
    "鸣潮": {
        "今汐", "长离", "折枝", "相里要", "守岸人", "釉瑚", "椿", "灯灯", "珂莱塔",
        "洛可可", "菲比", "忌炎", "吟霖", "维里奈", "安可", "鉴心", "卡卡罗", "凌阳",
        "散华", "丹瑾", "炽霞", "白芷", "秧秧", "桃祈", "莫特斐", "渊武", "秋水",
        "仇远", "卜灵", "绯雪", "西格莉卡"
    },
    "明日方舟": {
        "阿米娅", "凯尔希", "陈", "银灰", "能天使", "艾雅法拉", "伊芙利特", "塞雷娅",
        "星熊", "斯卡蒂", "黑", "麦哲伦", "莫斯提马", "煌", "阿", "年", "刻俄柏",
        "风笛", "温蒂", "早露", "铃兰", "棘刺", "森蚺", "史尔特尔", "瑕光", "泥岩",
        "迷迭香", "山", "空弦", "嵯峨", "异客", "歌蕾蒂娅", "浊心斯卡蒂", "卡涅利安",
        "帕拉斯", "水月", "假日威龙陈", "琴柳", "远牙", "焰尾", "耀骑士临光", "灵知",
        "令", "澄闪", "菲亚梅塔", "斥罪", "德克萨斯", "缄默德克萨斯", "玛恩纳", "提丰",
        "锏", "左乐", "维什戴尔", "逻各斯", "佩佩", "乌尔比安", "忍冬", "维娜",
        "荒芜拉普兰德", "不死途", "刻律德菈", "阿罗夏", "12F", "阿兰娜", "阿罗玛"
    }
}

TOTAL_ESTIMATE = 650  # 四大游戏全角色总预估数 (崩铁~90 + 原神~125 + 鸣潮~50 + 方舟~450)


def load_log_game_map() -> dict[str, str]:
    mapping = {}
    log_file = BOT_ROOT / "logs" / "download_character_refs.log"
    if log_file.is_file():
        try:
            import re
            for line in log_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = re.search(r"\[OK\]\s+\[(.*?)\]\s+(.*?)\s+->", line)
                if m:
                    g = m.group(1).strip()
                    c = m.group(2).strip()
                    if g in ("明日方舟", "方舟"):
                        mapping[c] = "明日方舟"
                    elif g in ("崩铁", "星铁", "崩坏：星穹铁道"):
                        mapping[c] = "崩坏：星穹铁道"
                    elif g in ("原神", "YS"):
                        mapping[c] = "原神"
                    elif g in ("鸣潮", "WuWa"):
                        mapping[c] = "鸣潮"
        except Exception:
            pass
    return mapping


def detect_game(char_name: str, log_map: dict[str, str] | None = None) -> str:
    if log_map and char_name in log_map:
        return log_map[char_name]
    for game, chars in GAME_KEYWORDS.items():
        if char_name in chars:
            return game
    return "综合角色"


def get_process_info() -> dict[str, Any]:
    """检查后台下载进程状态"""
    try:
        out = subprocess.check_output(
            ["pgrep", "-fl", "download_character_refs.py"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        lines = [line for line in out.splitlines() if line.strip()]
        if lines:
            pid = lines[0].split()[0]
            ps_out = subprocess.check_output(
                ["ps", "-p", pid, "-o", "%cpu,%mem,etime"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip().splitlines()
            if len(ps_out) > 1:
                parts = ps_out[1].split()
                return {
                    "running": True,
                    "pid": pid,
                    "cpu": parts[0] + "%",
                    "mem": parts[1] + "%",
                    "elapsed": parts[2],
                }
    except Exception:
        pass
    return {"running": False, "pid": "-", "cpu": "-", "mem": "-", "elapsed": "-"}


def get_controller_status() -> dict[str, Any]:
    """回读 NoneBot API 状态"""
    try:
        req = urllib.request.Request("http://127.0.0.1:8080/api/status", headers={"User-Agent": "Monitor"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return {}


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / (1024 * 1024):.2f} MB"


def render_progress_bar(current: int, total: int, width: int = 32) -> str:
    pct = min(1.0, current / max(1, total))
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{C_GREEN}{bar}{C_RESET}] {pct * 100:.1f}% ({current}/{total})"


def get_files_data() -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    files_info = []
    total_bytes = 0
    game_counts: dict[str, int] = {
        "崩坏：星穹铁道": 0,
        "原神": 0,
        "鸣潮": 0,
        "明日方舟": 0,
        "综合角色": 0,
    }
    log_map = load_log_game_map()

    for p in REFS_DIR.glob("*.png"):
        try:
            stat = p.stat()
            size = stat.st_size
            mtime = stat.st_mtime
            char_name = p.stem
            game = detect_game(char_name, log_map)
            game_counts[game] = game_counts.get(game, 0) + 1
            total_bytes += size
            files_info.append({
                "name": char_name,
                "file": p.name,
                "size": size,
                "mtime": mtime,
                "game": game,
            })
        except Exception:
            continue

    files_info.sort(key=lambda x: x["mtime"], reverse=True)
    return files_info, total_bytes, game_counts


def render_dashboard(last_count: int, last_time: float) -> tuple[int, float]:
    now = time.time()
    files_info, total_bytes, game_counts = get_files_data()
    proc = get_process_info()
    api_stat = get_controller_status()

    current_count = len(files_info)
    speed_text = ""
    if last_time > 0 and now > last_time:
        diff_count = current_count - last_count
        diff_sec = now - last_time
        if diff_sec > 0:
            cps = diff_count / diff_sec * 60
            speed_text = f"速度: ~{cps:.1f} 张/分钟"

    term_width = 80
    try:
        term_width = os.get_terminal_size().columns
    except Exception:
        pass

    sep = "─" * min(term_width, 82)
    double_sep = "═" * min(term_width, 82)

    buf = []
    buf.append("\033[H\033[2J")  # 清屏并光标归位
    buf.append(f"{C_BOLD}{C_CYAN}╔{double_sep[:78]}╗{C_RESET}")
    title = " PRTS // QQ Agent 四大游戏角色立绘实况下载大盘 "
    buf.append(f"{C_BOLD}{C_CYAN}║{C_RESET} {C_BOLD}{C_WHITE}{title.center(76)}{C_RESET} {C_BOLD}{C_CYAN}║{C_RESET}")
    buf.append(f"{C_BOLD}{C_CYAN}╚{double_sep[:78]}╝{C_RESET}")

    # 1. 后台引擎与状态
    p_status = f"{C_GREEN}● 正在高并发抓取中{C_RESET}" if proc["running"] else f"{C_YELLOW}○ 离线/就绪{C_RESET}"
    p_details = f"PID: {proc['pid']} | CPU: {proc['cpu']} | 内存: {proc['mem']} | 耗时: {proc['elapsed']}" if proc["running"] else "无后台下载任务在运行"
    buf.append(f"{C_BOLD}[下载引擎状态]{C_RESET} {p_status}  ({p_details})")

    # 2. 全局进度条与总存储
    bar_str = render_progress_bar(current_count, TOTAL_ESTIMATE, width=32)
    size_str = format_bytes(total_bytes)
    buf.append(f"{C_BOLD}[立绘总进度]{C_RESET}   {bar_str}  {C_CYAN}{size_str}{C_RESET}  {C_GREEN}{speed_text}{C_RESET}")
    buf.append(f"{C_DIM}{sep}{C_RESET}")

    # 3. 四大游戏分布统计
    buf.append(f"{C_BOLD}【四大游戏已就绪角色分布】{C_RESET}")
    g_hsr = f"{C_YELLOW}崩坏：星穹铁道{C_RESET}: {C_BOLD}{game_counts.get('崩坏：星穹铁道', 0)}{C_RESET} 位"
    g_ys = f"{C_BLUE}原神{C_RESET}: {C_BOLD}{game_counts.get('原神', 0)}{C_RESET} 位"
    g_wuwa = f"{C_MAGENTA}鸣潮{C_RESET}: {C_BOLD}{game_counts.get('鸣潮', 0)}{C_RESET} 位"
    g_ark = f"{C_GREEN}明日方舟{C_RESET}: {C_BOLD}{game_counts.get('明日方舟', 0)}{C_RESET} 位"
    buf.append(f"  {g_hsr}  |  {g_ys}  |  {g_wuwa}  |  {g_ark}")
    buf.append(f"{C_DIM}{sep}{C_RESET}")

    # 4. 最近入库动态 (最新 10 位角色)
    buf.append(f"{C_BOLD}【最新归档角色实时流水 (Latest 10)】{C_RESET}")
    recent = files_info[:10]
    if not recent:
        buf.append(f"  {C_DIM}暂无文件，等待写入...{C_RESET}")
    else:
        for item in recent:
            t_str = datetime.datetime.fromtimestamp(item["mtime"]).strftime("%H:%M:%S")
            sz_str = format_bytes(item["size"])
            g_tag = item["game"]
            if g_tag == "崩坏：星穹铁道":
                badge = f"{C_YELLOW}[崩铁]{C_RESET}"
            elif g_tag == "原神":
                badge = f"{C_BLUE}[原神]{C_RESET}"
            elif g_tag == "鸣潮":
                badge = f"{C_MAGENTA}[鸣潮]{C_RESET}"
            elif g_tag == "明日方舟":
                badge = f"{C_GREEN}[方舟]{C_RESET}"
            else:
                badge = f"{C_WHITE}[二游]{C_RESET}"

            c_name = f"{C_BOLD}{item['name']}{C_RESET}".ljust(18)
            buf.append(f"  [{t_str}]  {badge}  {c_name} -> {sz_str:>9}   {C_DIM}(bot/data/refs/{item['file']}){C_RESET}")

    buf.append(f"{C_DIM}{sep}{C_RESET}")

    # 5. 生图一致性联动核验
    api_ok = api_stat.get("global_active", False)
    saved_refs_len = len(api_stat.get("saved_refs", []))
    act_groups = api_stat.get("active_groups", [])
    bot_conn = api_stat.get("bot_connected", False)

    api_badge = f"{C_GREEN}正常在线{C_RESET}" if api_ok else f"{C_RED}离线{C_RESET}"
    qq_badge = f"{C_GREEN}已连接 OneBot{C_RESET}" if bot_conn else f"{C_YELLOW}未连 OneBot{C_RESET}"
    buf.append(f"{C_BOLD}【NoneBot2 智能体与生图一致性联动】{C_RESET}")
    buf.append(f"  服务状态: {api_badge}  |  QQ 连接: {qq_badge}  |  生图已索引参考图: {C_BOLD}{saved_refs_len}{C_RESET} 位")
    buf.append(f"  已激活白名单群: {C_CYAN}{', '.join(act_groups) or '无'}{C_RESET}")
    buf.append(f"{C_DIM}{double_sep}{C_RESET}")
    buf.append(f"{C_DIM}按 Ctrl+C 退出监控 | 本地存储: {REFS_DIR}{C_RESET}")

    sys.stdout.write("\n".join(buf) + "\n")
    sys.stdout.flush()
    return current_count, now


def main() -> None:
    parser = argparse.ArgumentParser(description="角色立绘实况下载大盘")
    parser.add_argument("--interval", type=float, default=2.0, help="刷新间隔秒数 (默认 2.0)")
    parser.add_argument("--once", action="store_true", help="仅打印一次当前进度快照后退出")
    args = parser.parse_args()

    if args.once:
        render_dashboard(0, 0)
        return

    last_count = 0
    last_time = 0.0
    try:
        while True:
            last_count, last_time = render_dashboard(last_count, last_time)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        sys.stdout.write(f"\n{C_CYAN}[PRTS] 进度监控已正常退出。{C_RESET}\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
