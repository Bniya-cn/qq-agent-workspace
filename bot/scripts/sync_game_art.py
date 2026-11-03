#!/usr/bin/env python3
"""按游戏归档并更新角色立绘；逐文件输出字节进度，保留原平铺素材。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image

import download_character_refs as legacy

ROOT = Path(__file__).resolve().parents[1] / 'data' / 'refs'
GAMES = {
    'ark': ('Arknights', legacy.fetch_arknights_characters),
    'wuwa': ('WutheringWaves', legacy.fetch_wuwa_characters),
    'hsr': ('HonkaiStarRail', legacy.fetch_hsr_characters),
    'ys': ('GenshinImpact', legacy.fetch_genshin_characters),
    'ba': ('BlueArchive', None),
    'ef': ('Endfield', None),
}
ENDFIELD_NAME_OVERRIDES = {
    'chr_0002_endminm': '管理员（男）',
    'chr_0003_endminf': '管理员（女）',
}
ENDFIELD_DATA = 'https://data.akedata.wiki'
BA_NAME_OVERRIDES = {
    '10153': ('日富美（赛车）', ['阿慈谷日富美（赛车）', '日富美赛车服']),
    '20063': ('白洲梓（赛车）', ['梓（赛车）', '白洲梓赛车服']),
    '26017': ('古畑兰娜', ['古畑拉娜', '拉娜']),
}


def save_catalog(path, catalog):
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


async def discover_ba(client, base):
    datasets = {}
    for lang in ('jp', 'en', 'cn'):
        response = await client.get(f'{base}/data/{lang}/students.json')
        response.raise_for_status()
        datasets[lang] = response.json()
    targets = []
    for sid, row in datasets['jp'].items():
        en = datasets['en'].get(sid, row)
        cn = datasets['cn'].get(sid, row)
        slug = en.get('PathName') or row.get('PathName') or str(sid)
        source_cn_name = str(cn.get('Name') or en.get('Name') or row.get('Name') or slug)
        cn_name, local_aliases = BA_NAME_OVERRIDES.get(str(sid), (source_cn_name, []))
        if source_cn_name != str(row.get('Name') or ''):
            cn_name = source_cn_name
            local_aliases = []
        aliases = list(dict.fromkeys(str(x) for x in (cn_name, *local_aliases, source_cn_name, en.get('Name'), row.get('Name'), slug) if x))
        safe_cn_name = re.sub(r'[\\/:*?"<>|]', '_', cn_name)
        targets.append({'id': str(sid), 'name': cn_name, 'aliases': aliases,
                        'filename': f'{sid}_{safe_cn_name}.webp',
                        'url': f'{base}/images/student/portrait/{sid}.webp',
                        'region': 'JP roster / SchaleDB portrait; not CN client assets'})
    return targets


async def discover_endfield(client):
    """终末地立绘来自 AKEData 当前 latest 版本的角色表，文件名用中文名。"""
    manifest = (await client.get(f'{ENDFIELD_DATA}/manifest.json')).json()
    latest_id = manifest.get('latest')
    version = next((item for item in manifest.get('versions', []) if item.get('id') == latest_id), None)
    if not version or not version.get('tableCfgPath'):
        raise RuntimeError(f'AKEData 未提供可用版本：{latest_id}')
    root = f'{ENDFIELD_DATA}/{version["tableCfgPath"].strip("/")}'
    characters = (await client.get(f'{root}/CharacterTable.json')).json()
    names = (await client.get(f'{root}/I18nTextTable_CN.json')).json()
    targets = []
    for char_id, row in characters.items():
        name_id = str((row.get('name') or {}).get('id') or '')
        source_name = str(names.get(name_id) or row.get('engName') or char_id)
        display_name = ENDFIELD_NAME_OVERRIDES.get(char_id, source_name)
        url = (
            f'{ENDFIELD_DATA}/public/images/assets/beyond/dynamicassets/gameplay/'
            f'ui/sprites/characterportrait/{char_id}.png'
        )
        head = await client.head(url)
        if head.status_code != 200:
            print(f'[跳过] Endfield {display_name} ({char_id}) 当前版本没有立绘', flush=True)
            continue
        aliases = list(dict.fromkeys(
            str(item) for item in (display_name, source_name, row.get('engName'), char_id) if item
        ))
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', display_name)
        targets.append({
            'id': char_id,
            'name': display_name,
            'aliases': aliases,
            'filename': f'{safe_name}.png',
            'url': url,
            'region': f'AKEData {latest_id} characterportrait; community data, not an official client export',
            'sort_order': row.get('sortOrder') or 0,
        })
    targets.sort(key=lambda item: (item['sort_order'], item['id']))
    if not targets:
        raise RuntimeError(f'AKEData {latest_id} 没有可下载的终末地立绘')
    return targets


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--games', default='all', help='all 或 ark,wuwa,hsr,ys,ba')
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--limit', type=int, default=0, help='每游戏抽样上限；0 为源目录全部')
    parser.add_argument('--list-only', action='store_true', help='只生成目录，不下载图片')
    parser.add_argument('--schale-base', default='https://schaledb.com')
    args = parser.parse_args()
    games = list(GAMES) if args.games == 'all' else args.games.split(',')
    if any(g not in GAMES for g in games) or not 1 <= args.workers <= 8 or args.limit < 0:
        parser.error('游戏代号无效，workers 要求 1–8，limit 要求非负')
    ROOT.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(args.workers)
    summary = {'downloaded': 0, 'unchanged': 0, 'failed': 0}
    timeout = httpx.Timeout(180 if 'ef' in games else 30, connect=15)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for game in games:
            folder, discover = GAMES[game]
            print(f'\n[枚举] {folder} 正在查询最新源目录…', flush=True)
            heartbeat_stop = asyncio.Event()
            async def heartbeat():
                while not heartbeat_stop.is_set():
                    try:
                        await asyncio.wait_for(heartbeat_stop.wait(), 5)
                    except asyncio.TimeoutError:
                        print(f'[枚举] {folder} 查询中，网络请求尚未结束…', flush=True)
            pulse = asyncio.create_task(heartbeat())
            try:
                if game == 'ba':
                    targets = await discover_ba(client, args.schale_base.rstrip('/'))
                elif game == 'ef':
                    targets = await discover_endfield(client)
                else:
                    rows = await discover(client, limit=0)
                    targets = [{'id': name, 'name': name, 'aliases': [name],
                                'filename': re.sub(r'[\\/:*?"<>|]', '_', name) + '.png',
                                'url': url, 'region': 'wiki'} for name, url in rows]
                if not targets:
                    raise RuntimeError('源未返回可用立绘，不能视为同步完成')
            except (httpx.HTTPError, ValueError, RuntimeError) as exc:
                print(f'[失败] {folder} 目录：{exc}', flush=True)
                summary['failed'] += 1
                continue
            finally:
                heartbeat_stop.set()
                await pulse
            if args.limit:
                targets = targets[:args.limit]
            directory = ROOT / folder
            directory.mkdir(exist_ok=True)
            catalog_path = directory / 'catalog.json'
            catalog = json.loads(catalog_path.read_text()) if catalog_path.exists() else {'game': folder, 'assets': {}}
            for item in targets:
                prior = catalog['assets'].get(item['id'], {})
                catalog['assets'][item['id']] = {**prior, **item}
            save_catalog(catalog_path, catalog)
            print(f'[目录] {folder}: 本次 {len(targets)} 张；完整性仅相对于此来源返回目录', flush=True)
            if args.list_only:
                continue
            completed = 0
            async def download(item):
                nonlocal completed
                async with sem:
                    target = directory / item['filename']
                    # 分类目录独立保存，旧平铺文件留待经过校验的清理流程处理。
                    old = ROOT / item['filename']
                    if not target.exists() and old.is_file():
                        shutil.copy2(old, target)
                    entry = catalog['assets'][item['id']]
                    remote_sha1 = legacy.IMAGE_SHA1_BY_URL.get(item['url'])
                    if target.is_file() and remote_sha1 and hashlib.sha1(target.read_bytes()).hexdigest() == remote_sha1:
                        entry.update(status='ready', sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                                     bytes=target.stat().st_size, checked_at=datetime.now(timezone.utc).isoformat())
                        entry.pop('error', None)
                        save_catalog(catalog_path, catalog)
                        summary['unchanged'] += 1
                        completed += 1
                        print(f'[进度] {folder} {completed}/{len(targets)} ({completed/len(targets):.1%}) {item["name"]} 远端哈希相同，跳过下载', flush=True)
                        return
                    headers = legacy.get_headers(item['url'])
                    if target.exists() and entry.get('sha256') == hashlib.sha256(target.read_bytes()).hexdigest():
                        if entry.get('etag'): headers['If-None-Match'] = entry['etag']
                        if entry.get('last_modified'): headers['If-Modified-Since'] = entry['last_modified']
                    part = target.with_suffix(target.suffix + '.part')
                    for attempt in range(3):
                        try:
                            async with client.stream('GET', item['url'], headers=headers) as response:
                                if response.status_code == 304:
                                    summary['unchanged'] += 1
                                    entry['status'] = 'ready'
                                    entry.pop('error', None)
                                    break
                                response.raise_for_status()
                                total = int(response.headers.get('content-length') or 0)
                                received = 0
                                last_report = 0.0
                                with part.open('wb') as out:
                                    async for chunk in response.aiter_bytes():
                                        out.write(chunk)
                                        received += len(chunk)
                                        now = time.monotonic()
                                        if now - last_report >= 1:
                                            pct = f'{received / total:.0%}' if total else '总大小未知'
                                            print(f'[传输] {folder}/{item["name"]}: {received/1024:.0f}KB {pct}', flush=True)
                                            last_report = now
                                with Image.open(part) as image:
                                    image.verify()
                                digest = hashlib.sha256(part.read_bytes()).hexdigest()
                                unchanged = target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == digest
                                part.replace(target)
                                summary['unchanged' if unchanged else 'downloaded'] += 1
                                entry.update(sha256=digest, bytes=received, etag=response.headers.get('etag'),
                                             last_modified=response.headers.get('last-modified'))
                                entry['status'] = 'ready'
                                entry.pop('error', None)
                                break
                        except (httpx.HTTPError, OSError, ValueError, SyntaxError) as exc:
                            part.unlink(missing_ok=True)
                            print(f'[重试 {attempt+1}/3] {folder}/{item["name"]}: {type(exc).__name__}: {exc}', flush=True)
                            if attempt == 2:
                                entry['status'] = 'failed'
                                entry['error'] = str(exc)
                                summary['failed'] += 1
                            else:
                                await asyncio.sleep(attempt + 1)
                    entry['checked_at'] = datetime.now(timezone.utc).isoformat()
                    save_catalog(catalog_path, catalog)
                    completed += 1
                    print(f'[进度] {folder} {completed}/{len(targets)} ({completed/len(targets):.1%}) {item["name"]} {entry["status"]}', flush=True)
            await asyncio.gather(*(download(item) for item in targets))
    print('\n[汇总] ' + json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
