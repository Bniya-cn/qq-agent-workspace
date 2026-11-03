import hashlib
import os
import shutil

src_base = '/Users/daijinglin/Desktop/qq-agent-workspace'
dst_base = '/Users/daijinglin/Library/CloudStorage/GoogleDrive-bniya050402@gmail.com/我的云端硬盘/inner-innermountain-uniapp/qq-agent-sync'

sync_files = [
    'bot/data/presets/amiya.txt',
    'bot/src/plugins/agent_core/modes/small_talk.py',
    'bot/src/plugins/agent_core/llm_service.py',
    'bot/src/plugins/agent_core/web_panel.py',
    'bot/src/plugins/agent_core/web/index.html',
    'bot/src/plugins/agent_core/web/panel.js',
    'bot/src/plugins/agent_core/web/panel.css',
    'bot/src/plugins/agent_core/web/theme.js',
    'bot/src/plugins/agent_core/web/README.md',
    'bot/src/plugins/agent_core/local_rag_service.py',
    'bot/tests/test_persona_panel_daily.py',
]

for rel in sync_files:
    sp = os.path.join(src_base, rel)
    dp = os.path.join(dst_base, rel)
    if not os.path.exists(sp):
        continue
    os.makedirs(os.path.dirname(dp), exist_ok=True)
    shutil.copy2(sp, dp)
    s_size = os.path.getsize(sp)
    d_size = os.path.getsize(dp)
    s_h = hashlib.sha256(open(sp, 'rb').read()).hexdigest()
    d_h = hashlib.sha256(open(dp, 'rb').read()).hexdigest()
    print(f"SYNCED: {rel} | SRC: {s_size}B | DST: {d_size}B | SHA256_MATCH: {s_h == d_h} | SHA: {s_h[:12]}")
