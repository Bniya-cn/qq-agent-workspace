#!/usr/bin/env python3
"""全自动四大二次元游戏（明日方舟、鸣潮、崩铁、原神）官方高清角色立绘批量归档工具。

数据源契约：
- 明日方舟: PRTS Wiki API (https://prts.wiki/api.php) -> File:立绘_{干员名}_1.png / 2.png
- 崩坏：星穹铁道: BWiki SR API (https://wiki.biligame.com/sr/api.php) -> File:{角色名}立绘.png
- 原神: BWiki YS API (https://wiki.biligame.com/ys/api.php) -> File:无背景-角色-{角色名}.png / File:{角色名}立绘.png
- 鸣潮: BWiki WuWa API (https://wiki.biligame.com/wutheringwaves/api.php) -> File:角色_{角色名}_立绘.png

存储目标:
- bot/data/refs/{角色名}.png
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

import httpx

# 强制优先使用 IPv4 DNS 解析，彻底避免国内 Wiki (如 PRTS/BiliWiki) 在 macOS 上的 IPv6 握手超时
_orig_loop_getaddrinfo = asyncio.BaseEventLoop.getaddrinfo
asyncio.BaseEventLoop.getaddrinfo = (
    lambda self, host, port, family=0, type=0, proto=0, flags=0: _orig_loop_getaddrinfo(
        self, host, port, family=socket.AF_INET, type=type, proto=proto, flags=flags
    )
)

# 脚本位于 bot/scripts/ -> 项目根目录 bot/
BOT_ROOT = Path(__file__).resolve().parents[1]
REFS_DIR = BOT_ROOT / "data" / "refs"

def get_headers(url: str) -> dict[str, str]:
    if "prts.wiki" in url:
        return {"User-Agent": "curl/8.7.1"}
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://wiki.biligame.com/",
    }

SEM = asyncio.Semaphore(12)
IMAGE_SHA1_BY_URL: dict[str, str] = {}


async def _fetch_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    # 分类超过单页上限时继续读取，避免漏掉后续新增角色。
    if params.get("list") == "categorymembers":
        combined: dict[str, Any] = {"query": {"categorymembers": []}}
        page_params = dict(params)
        while True:
            response = await client.get(url, params=page_params, headers=get_headers(url))
            response.raise_for_status()
            page = response.json()
            if "error" in page:
                raise RuntimeError(str(page["error"]))
            combined["query"]["categorymembers"].extend(page.get("query", {}).get("categorymembers", []))
            if not page.get("continue"):
                return combined
            page_params.update(page["continue"])
    if params.get("prop") == "imageinfo":
        params = {**params, "iiprop": "url|sha1"}
    for attempt in range(3):
        try:
            resp = await client.get(url, params=params, headers=get_headers(url))
            if resp.status_code == 200:
                data = resp.json()
                for page in data.get("query", {}).get("pages", {}).values():
                    for image in page.get("imageinfo", []):
                        if image.get("url") and image.get("sha1"):
                            value = image["sha1"]
                            IMAGE_SHA1_BY_URL[image["url"]] = value.lower() if len(value) == 40 else f"{int(value, 36):040x}"
                return data
        except Exception as e:
            if attempt == 2:
                print(f"[WARN] 请求失败 {url} params={params}: {e}", file=sys.stderr, flush=True)
            await asyncio.sleep(0.5 + attempt * 0.5)
    raise RuntimeError(f"立绘目录查询失败: {url}")


async def _download_file(
    client: httpx.AsyncClient,
    image_url: str,
    target_path: Path,
    label: str,
) -> bool:
    if target_path.is_file() and target_path.stat().st_size > 50_000:
        return False  # 已存在跳过

    async with SEM:
        for attempt in range(3):
            try:
                resp = await client.get(image_url, headers=get_headers(image_url), follow_redirects=True, timeout=25.0)
                if resp.status_code == 200 and len(resp.content) > 50_000:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_bytes(resp.content)
                    size_kb = len(resp.content) // 1024
                    print(f"  [OK] {label} -> {target_path.name} ({size_kb} KB)", flush=True)
                    return True
            except Exception as e:
                if attempt == 2:
                    print(f"  [FAIL] {label} 下载失败 ({image_url}): {e}", flush=True)
                await asyncio.sleep(0.5 + attempt * 0.5)
    return False


# ---------------------------------------------------------------------------
# 1. 崩坏：星穹铁道 (HSR)
# ---------------------------------------------------------------------------
async def fetch_hsr_characters(client: httpx.AsyncClient, limit: int = 0) -> list[tuple[str, str]]:
    """返回 [(角色名, 图片下载URL)]"""
    api = "https://wiki.biligame.com/sr/api.php"
    params = {
        "action": "query",
        "format": "json",
        "list": "categorymembers",
        "cmtitle": "分类:角色",
        "cmlimit": 500,
    }
    data = await _fetch_json(client, api, params)
    members = data.get("query", {}).get("categorymembers", [])
    
    char_names: list[str] = []
    ignore_prefixes = ("分类:", "模板:", "模块:", "列表", "UP记录", "Archer")
    for m in members:
        name = m.get("title", "").strip()
        if not name or any(name.startswith(p) for p in ignore_prefixes):
            continue
        # 清洗特殊后缀
        clean_name = re.sub(r"•.*", "", name).strip()
        if clean_name and clean_name not in char_names:
            char_names.append(clean_name)

    if limit > 0:
        char_names = char_names[:limit]

    print(f"\n[星铁] 发现 {len(char_names)} 个候选角色，正在查询立绘直链...")

    results: list[tuple[str, str]] = []
    # 批量 40 个一次查询 imageinfo
    chunk_size = 40
    for i in range(0, len(char_names), chunk_size):
        chunk = char_names[i : i + chunk_size]
        title_map = {f"File:{c}立绘.png": c for c in chunk}
        info_params = {
            "action": "query",
            "format": "json",
            "titles": "|".join(title_map.keys()),
            "prop": "imageinfo",
            "iiprop": "url",
        }
        res = await _fetch_json(client, api, info_params)
        pages = res.get("query", {}).get("pages", {})
        for _, pinfo in pages.items():
            t = pinfo.get("title", "")
            # normalized 或原始
            char_name = None
            for k, c in title_map.items():
                if k.split(":", 1)[-1].replace(" ", "_") == t.split(":", 1)[-1].replace(" ", "_"):
                    char_name = c
                    break
            if not char_name:
                continue
            infos = pinfo.get("imageinfo", [])
            if infos and "url" in infos[0]:
                results.append((char_name, infos[0]["url"]))
    return results


# ---------------------------------------------------------------------------
# 2. 原神 (Genshin Impact)
# ---------------------------------------------------------------------------
async def fetch_genshin_characters(client: httpx.AsyncClient, limit: int = 0) -> list[tuple[str, str]]:
    """返回 [(角色名, 图片下载URL)]，优先「无背景-角色-XX.png」，回退「XX立绘.png」"""
    api = "https://wiki.biligame.com/ys/api.php"
    params = {
        "action": "query",
        "format": "json",
        "list": "categorymembers",
        "cmtitle": "分类:角色",
        "cmlimit": 500,
    }
    data = await _fetch_json(client, api, params)
    members = data.get("query", {}).get("categorymembers", [])
    
    char_names: list[str] = []
    ignore_prefixes = ("分类:", "模板:", "模块:", "旅行者", "图鉴")
    for m in members:
        name = m.get("title", "").strip()
        if not name or any(name.startswith(p) for p in ignore_prefixes):
            continue
        clean_name = re.sub(r"\(.*?\)", "", name).strip()
        if clean_name and clean_name not in char_names:
            char_names.append(clean_name)

    if limit > 0:
        char_names = char_names[:limit]

    print(f"\n[原神] 发现 {len(char_names)} 个候选角色，正在查询透明/高清立绘直链...")

    results: list[tuple[str, str]] = []
    chunk_size = 40
    for i in range(0, len(char_names), chunk_size):
        chunk = char_names[i : i + chunk_size]
        title_map = {f"File:{c}立绘.png": c for c in chunk}

        info_params = {
            "action": "query",
            "format": "json",
            "titles": "|".join(title_map.keys()),
            "prop": "imageinfo",
            "iiprop": "url",
        }
        res = await _fetch_json(client, api, info_params)
        pages = res.get("query", {}).get("pages", {})
        for _, pinfo in pages.items():
            t = pinfo.get("title", "")
            infos = pinfo.get("imageinfo", [])
            if infos and "url" in infos[0]:
                for source_title, cname in title_map.items():
                    if source_title.split(":", 1)[-1].replace(" ", "_") == t.split(":", 1)[-1].replace(" ", "_"):
                        results.append((cname, infos[0]["url"]))
                        break
    return results


# ---------------------------------------------------------------------------
# 3. 鸣潮 (Wuthering Waves)
# ---------------------------------------------------------------------------
async def fetch_wuwa_characters(client: httpx.AsyncClient, limit: int = 0) -> list[tuple[str, str]]:
    """返回 [(角色名, 图片下载URL)]"""
    api = "https://wiki.biligame.com/wutheringwaves/api.php"
    params = {
        "action": "parse",
        "page": "共鸣者",
        "prop": "links",
        "format": "json",
    }
    data = await _fetch_json(client, api, params)
    links = data.get("parse", {}).get("links", [])

    char_names: list[str] = []
    for l in links:
        t = l.get("*", "").strip()
        if t.startswith("共鸣者/"):
            sub = t.split("/", 1)[1].strip()
            if "/" not in sub and "·" not in sub and sub not in char_names:
                char_names.append(sub)

    # 兜底核心与最新列表
    fallback_chars = [
        "今汐", "长离", "折枝", "相里要", "守岸人", "釉瑚", "椿", "灯灯", "珂莱塔",
        "洛可可", "菲比", "忌炎", "吟霖", "维里奈", "安可", "鉴心", "卡卡罗", "凌阳",
        "散华", "丹瑾", "炽霞", "白芷", "秧秧", "桃祈", "莫特斐", "渊武", "秋水"
    ]
    for fc in fallback_chars:
        if fc not in char_names:
            char_names.append(fc)

    if limit > 0:
        char_names = char_names[:limit]

    print(f"\n[鸣潮] 发现 {len(char_names)} 个候选共鸣者，正在查询立绘直链...")

    results: list[tuple[str, str]] = []
    chunk_size = 40
    for i in range(0, len(char_names), chunk_size):
        chunk = char_names[i : i + chunk_size]
        title_map = {f"File:角色_{c}_立绘.png": c for c in chunk}
        info_params = {
            "action": "query",
            "format": "json",
            "titles": "|".join(title_map.keys()),
            "prop": "imageinfo",
            "iiprop": "url",
        }
        res = await _fetch_json(client, api, info_params)
        pages = res.get("query", {}).get("pages", {})
        for _, pinfo in pages.items():
            t = pinfo.get("title", "")
            infos = pinfo.get("imageinfo", [])
            if infos and "url" in infos[0]:
                for source_title, cname in title_map.items():
                    if source_title.split(":", 1)[-1].replace(" ", "_") == t.split(":", 1)[-1].replace(" ", "_"):
                        results.append((cname, infos[0]["url"]))
                        break
    return results


# ---------------------------------------------------------------------------
# 4. 明日方舟 (Arknights)
# ---------------------------------------------------------------------------
async def fetch_arknights_characters(client: httpx.AsyncClient, limit: int = 0) -> list[tuple[str, str]]:
    """返回 [(干员名, 图片下载URL)]，使用 PRTS.wiki 原生超高清透明立绘"""
    api = "https://prts.wiki/api.php"
    params = {
        "action": "query",
        "format": "json",
        "list": "categorymembers",
        "cmtitle": "分类:干员",
        "cmlimit": 500,
    }
    data = await _fetch_json(client, api, params)
    members = data.get("query", {}).get("categorymembers", [])

    char_names: list[str] = []
    ignore_prefixes = ("分类:", "模板:", "模块:", "干员索引")
    for m in members:
        name = m.get("title", "").strip()
        if not name or any(name.startswith(p) for p in ignore_prefixes):
            continue
        clean_name = re.sub(r"\(.*?\)", "", name).strip()
        if clean_name and clean_name not in char_names:
            char_names.append(clean_name)

    if limit > 0:
        char_names = char_names[:limit]

    print(f"\n[明日方舟] 发现 {len(char_names)} 个候选干员，正在查询 PRTS 官方透明立绘直链...")

    results: list[tuple[str, str]] = []
    chunk_size = 40
    for i in range(0, len(char_names), chunk_size):
        chunk = char_names[i : i + chunk_size]
        title_map = {}
        for c in chunk:
            title_map[f"File:立绘_{c}_1.png"] = c

        info_params = {
            "action": "query",
            "format": "json",
            "titles": "|".join(title_map.keys()),
            "prop": "imageinfo",
            "iiprop": "url",
        }
        res = await _fetch_json(client, api, info_params)
        pages = res.get("query", {}).get("pages", {})
        for _, pinfo in pages.items():
            t = pinfo.get("title", "")
            infos = pinfo.get("imageinfo", [])
            if infos and "url" in infos[0]:
                for source_title, cname in title_map.items():
                    if source_title.split(":", 1)[-1].replace(" ", "_") == t.split(":", 1)[-1].replace(" ", "_"):
                        results.append((cname, infos[0]["url"]))
                        break
    return results


# ---------------------------------------------------------------------------
# 主流程与并发下载
# ---------------------------------------------------------------------------
async def main() -> None:
    parser = argparse.ArgumentParser(description="四大二次元游戏官方全角色立绘批量下载器")
    parser.add_argument(
        "--games",
        default="all",
        help="目标游戏：all 或逗号分隔 (hsr,ys,wuwa,ark)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="每个游戏下载数量上限 (0 为不限制全量)",
    )
    args = parser.parse_args()

    games_to_run = set(
        ["hsr", "ys", "wuwa", "ark"]
        if args.games.strip().lower() == "all"
        else [g.strip().lower() for g in args.games.split(",")]
    )

    REFS_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print(f"二次元角色立绘自动归档工具已启动")
    print(f"目标存储目录: {REFS_DIR}")
    print(f"目标游戏范围: {sorted(games_to_run)}, 单游戏上限: {args.limit or '全量'}")
    print("=" * 60)

    # 显式使用 trust_env=False 直连国内 Wiki CDN，防止系统全局代理拦截
    async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
        all_targets: list[tuple[str, str, str]] = []  # (游戏名, 角色名, URL)

        if "hsr" in games_to_run:
            hsr_list = await fetch_hsr_characters(client, limit=args.limit)
            all_targets.extend([("崩铁", c, u) for c, u in hsr_list])

        if "ys" in games_to_run:
            ys_list = await fetch_genshin_characters(client, limit=args.limit)
            all_targets.extend([("原神", c, u) for c, u in ys_list])

        if "wuwa" in games_to_run:
            wuwa_list = await fetch_wuwa_characters(client, limit=args.limit)
            all_targets.extend([("鸣潮", c, u) for c, u in wuwa_list])

        if "ark" in games_to_run:
            ark_list = await fetch_arknights_characters(client, limit=args.limit)
            all_targets.extend([("明日方舟", c, u) for c, u in ark_list])

        print(f"\n[汇总] 共成功解析到 {len(all_targets)} 位角色的官方立绘直链，开始并发下载归档...")

        download_tasks = []
        for game_name, char_name, url in all_targets:
            target_file = REFS_DIR / f"{char_name}.png"
            label = f"[{game_name}] {char_name}"
            download_tasks.append(_download_file(client, url, target_file, label))

        results = await asyncio.gather(*download_tasks)
        downloaded = sum(1 for r in results if r)
        total = len(all_targets)

    print("\n" + "=" * 60)
    print(f"归档完成！本次下载新立绘: {downloaded} 张，总就绪角色立绘: {len(list(REFS_DIR.glob('*.png')))} 张")
    print(f"本地参考目录: {REFS_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
