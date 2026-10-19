"""四大游戏（明日方舟、鸣潮、崩铁、原神）官方全角色透明立绘后台抓取服务。"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import httpx
from nonebot import logger

from .config_manager import BOT_ROOT

REFS_DIR = BOT_ROOT / "data" / "refs"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

SEM = asyncio.Semaphore(6)

_fetcher_state: dict[str, Any] = {
    "status": "idle",  # "idle" | "running" | "completed" | "error"
    "current_game": "",
    "total": 0,
    "completed": 0,
    "downloaded_new": 0,
    "message": "就绪",
    "last_error": "",
}


def get_fetcher_status() -> dict[str, Any]:
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    count = len(list(REFS_DIR.glob("*.png")))
    st = dict(_fetcher_state)
    st["total_refs_on_disk"] = count
    return st


async def _fetch_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    for attempt in range(3):
        try:
            resp = await client.get(url, params=params, headers=HEADERS)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            if attempt == 2:
                logger.warning(f"[ref_fetcher] 请求失败 {url}: {e}")
            await asyncio.sleep(1.0 + attempt * 1.5)
    return {}


async def _download_file(
    client: httpx.AsyncClient,
    image_url: str,
    target_path: Path,
    label: str,
) -> bool:
    global _fetcher_state
    if target_path.is_file() and target_path.stat().st_size > 50_000:
        _fetcher_state["completed"] = _fetcher_state.get("completed", 0) + 1
        return False  # 已存在且大于50KB跳过

    async with SEM:
        for attempt in range(3):
            try:
                resp = await client.get(image_url, headers=HEADERS, follow_redirects=True)
                if resp.status_code == 200 and len(resp.content) > 50_000:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_bytes(resp.content)
                    size_kb = len(resp.content) // 1024
                    logger.info(f"[ref_fetcher] 已归档 {label} -> {target_path.name} ({size_kb} KB)")
                    _fetcher_state["completed"] = _fetcher_state.get("completed", 0) + 1
                    _fetcher_state["downloaded_new"] = _fetcher_state.get("downloaded_new", 0) + 1
                    return True
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"[ref_fetcher] {label} 下载失败 ({image_url}): {e}")
                await asyncio.sleep(1.0 + attempt * 1.0)
    _fetcher_state["completed"] = _fetcher_state.get("completed", 0) + 1
    return False


async def _fetch_hsr(client: httpx.AsyncClient, limit: int = 0) -> list[tuple[str, str]]:
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
        clean_name = re.sub(r"•.*", "", name).strip()
        if clean_name and clean_name not in char_names:
            char_names.append(clean_name)

    if limit > 0:
        char_names = char_names[:limit]

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
            char_name = None
            for k, c in title_map.items():
                if k in t or c in t:
                    char_name = c
                    break
            if not char_name:
                continue
            infos = pinfo.get("imageinfo", [])
            if infos and "url" in infos[0]:
                results.append((char_name, infos[0]["url"]))
    return results


async def _fetch_genshin(client: httpx.AsyncClient, limit: int = 0) -> list[tuple[str, str]]:
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
                for _, cname in title_map.items():
                    if cname in t:
                        results.append((cname, infos[0]["url"]))
                        break
    return results


async def _fetch_wuwa(client: httpx.AsyncClient, limit: int = 0) -> list[tuple[str, str]]:
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

    results: list[tuple[str, str]] = []
    title_map = {f"File:角色_{c}_立绘.png": c for c in char_names}
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
            for _, cname in title_map.items():
                if cname in t:
                    results.append((cname, infos[0]["url"]))
                    break
    return results


async def _fetch_arknights(client: httpx.AsyncClient, limit: int = 0) -> list[tuple[str, str]]:
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

    results: list[tuple[str, str]] = []
    chunk_size = 40
    for i in range(0, len(char_names), chunk_size):
        chunk = char_names[i : i + chunk_size]
        title_map = {f"File:立绘_{c}_1.png": c for c in chunk}

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
                for _, cname in title_map.items():
                    if cname in t:
                        results.append((cname, infos[0]["url"]))
                        break
    return results


async def run_fetch_task(games: str = "all", limit: int = 0) -> dict[str, Any]:
    global _fetcher_state
    _fetcher_state["status"] = "running"
    _fetcher_state["completed"] = 0
    _fetcher_state["downloaded_new"] = 0
    _fetcher_state["message"] = "正在解析四大游戏角色数据..."

    games_to_run = set(
        ["hsr", "ys", "wuwa", "ark"]
        if games.strip().lower() == "all"
        else [g.strip().lower() for g in games.split(",")]
    )

    REFS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
            all_targets: list[tuple[str, str, str]] = []

            if "hsr" in games_to_run:
                _fetcher_state["current_game"] = "崩坏：星穹铁道"
                hsr_list = await _fetch_hsr(client, limit=limit)
                all_targets.extend([("崩铁", c, u) for c, u in hsr_list])

            if "ys" in games_to_run:
                _fetcher_state["current_game"] = "原神"
                ys_list = await _fetch_genshin(client, limit=limit)
                all_targets.extend([("原神", c, u) for c, u in ys_list])

            if "wuwa" in games_to_run:
                _fetcher_state["current_game"] = "鸣潮"
                wuwa_list = await _fetch_wuwa(client, limit=limit)
                all_targets.extend([("鸣潮", c, u) for c, u in wuwa_list])

            if "ark" in games_to_run:
                _fetcher_state["current_game"] = "明日方舟"
                ark_list = await _fetch_arknights(client, limit=limit)
                all_targets.extend([("明日方舟", c, u) for c, u in ark_list])

            _fetcher_state["total"] = len(all_targets)
            _fetcher_state["message"] = f"成功获取 {len(all_targets)} 位角色链接，开始下载..."

            download_tasks = []
            for game_name, char_name, url in all_targets:
                target_file = REFS_DIR / f"{char_name}.png"
                label = f"[{game_name}] {char_name}"
                download_tasks.append(_download_file(client, url, target_file, label))

            results = await asyncio.gather(*download_tasks)
            downloaded = sum(1 for r in results if r)

            _fetcher_state["status"] = "completed"
            _fetcher_state["completed"] = len(all_targets)
            _fetcher_state["downloaded_new"] = downloaded
            _fetcher_state["message"] = f"全量归档完毕！新增 {downloaded} 张，当前总就绪 {len(list(REFS_DIR.glob('*.png')))} 张"
            logger.info(f"[ref_fetcher] {_fetcher_state['message']}")
            return get_fetcher_status()
    except Exception as exc:
        _fetcher_state["status"] = "error"
        _fetcher_state["last_error"] = str(exc)
        _fetcher_state["message"] = f"抓取异常: {exc}"
        logger.error(f"[ref_fetcher] 异常: {exc}")
        return get_fetcher_status()


def start_background_fetch(games: str = "all", limit: int = 0) -> dict[str, Any]:
    global _fetcher_state
    if _fetcher_state["status"] == "running":
        return get_fetcher_status()
    _fetcher_state["status"] = "running"
    _fetcher_state["completed"] = 0
    _fetcher_state["downloaded_new"] = 0
    _fetcher_state["message"] = f"正在启动四大游戏（明日方舟/鸣潮/崩铁/原神）角色立绘抓取（范围: {games}）..."
    asyncio.create_task(run_fetch_task(games=games, limit=limit))
    return get_fetcher_status()
