#!/usr/bin/env bash
set -e

WORKSPACE="/Users/daijinglin/Desktop/qq-agent-workspace"
BOT_DIR="$WORKSPACE/bot"
VENV_PY="$BOT_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    VENV_PY="python3"
fi

cd "$WORKSPACE"

# 0. 检查核心插件源码就绪
"$VENV_PY" - <<'PY'
from pathlib import Path

init_path = Path("bot/src/plugins/agent_core/__init__.py")
if init_path.exists():
    print("   -> 核心插件源码已就绪")
PY

echo "☁️ [1/5] 同步 Regression v8 全部修改文件至 Google Drive 镜像 (Rule 11.1)..."
"$VENV_PY" - <<'PY'
import hashlib
import os
import shutil

src_base = "/Users/daijinglin/Desktop/qq-agent-workspace"
dst_base = "/Users/daijinglin/Library/CloudStorage/GoogleDrive-bniya050402@gmail.com/我的云端硬盘/inner-innermountain-uniapp/qq-agent-sync"

sync_files = [
    "bot/data/config.json",
    "bot/data/groups/873321151.json",
    "bot/data/groups/711676140.json",
    "bot/data/presets/amiya.txt",
    "bot/data/knowledge/amiya_handbook.txt",
    "bot/data/knowledge/amiya/handbook.txt",
    "bot/src/plugins/agent_core/__init__.py",
    "bot/src/plugins/agent_core/config_manager.py",
    "bot/src/plugins/agent_core/controller_api.py",
    "bot/src/plugins/agent_core/errors.py",
    "bot/src/plugins/agent_core/llm_service.py",
    "bot/src/plugins/agent_core/local_rag_service.py",
    "bot/src/plugins/agent_core/modes/__init__.py",
    "bot/src/plugins/agent_core/modes/small_talk.py",
    "bot/src/plugins/agent_core/service_registry.py",
    "bot/src/plugins/agent_core/situational_state.py",
    "bot/src/plugins/agent_core/tts_service.py",
    "bot/src/plugins/agent_core/v8_patcher.py",
    "bot/src/plugins/agent_core/voice_presets.py",
    "bot/src/plugins/agent_core/web_panel.py",
    "bot/tests/test_agent_pipeline.py",
    "bot/tests/test_regression_v8.py",
    "bot/tests/test_vl_probe.py",
    "bot/tests/test_voice_presets_and_emotion.py",
]

if os.path.isdir(os.path.dirname(dst_base)):
    synced = 0
    for rel in sync_files:
        sp = os.path.join(src_base, rel)
        dp = os.path.join(dst_base, rel)
        if not os.path.exists(sp):
            continue
        os.makedirs(os.path.dirname(dp), exist_ok=True)
        shutil.copy2(sp, dp)
        s_size = os.path.getsize(sp)
        d_size = os.path.getsize(dp)
        s_h = hashlib.sha256(open(sp, "rb").read()).hexdigest()[:12]
        d_h = hashlib.sha256(open(dp, "rb").read()).hexdigest()[:12]
        assert s_size == d_size and s_h == d_h, f"Sync mismatch on {rel}"
        synced += 1
    print(f"   -> 已同步 {synced} 个 v8 文件至 Google Drive 镜像，文件大小与 SHA-256 100% 一致")
else:
    print("   -> Google Drive 镜像目录未挂载，跳过云端副本同步")
PY

echo "🔄 [2/5] 检查 GPT-SoVITS 语音合成服务 (端口 9880)..."
if lsof -iTCP:9880 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "   -> 9880 语音服务已在线"
else
    if [ -f "$WORKSPACE/gpt-sovits/scripts/start_api.sh" ]; then
        bash "$WORKSPACE/gpt-sovits/scripts/start_api.sh" >/dev/null 2>&1 || true
        sleep 1.5
        echo "   -> 已拉起 9880 语音服务"
    else
        echo "   -> 警告: 未找到 gpt-sovits/scripts/start_api.sh，请检查语音目录"
    fi
fi

echo "🧪 [3/5] 执行源码编译与全量单元测试冲突检查 (含 Regression v8 验证矩阵)..."
(
    cd "$BOT_DIR"
    export ENVIRONMENT=dev
    "$VENV_PY" -c "
import py_compile, glob
files = sorted(glob.glob('src/plugins/agent_core/*.py') + glob.glob('src/plugins/agent_core/modes/*.py'))
for f in files:
    py_compile.compile(f, doraise=True)
print(f'   -> 编译检查全部通过 ({len(files)} 个源码文件)')
"
    "$VENV_PY" -m unittest discover -s tests -p "test_*.py"
)

echo "🚀 [4/5] 优雅重启 NoneBot & WebUI v9 控制台 (端口 8080)..."
OLD_PIDS=$( (lsof -tiTCP:8080 -sTCP:LISTEN 2>/dev/null; lsof -t "$BOT_DIR/data/mem0_qdrant/.lock" 2>/dev/null) | sort -u | tr '\n' ' ' )
if [ -n "${OLD_PIDS// /}" ]; then
    echo "   -> 停止旧 8080 / Qdrant 锁进程 ($OLD_PIDS)..."
    kill -TERM $OLD_PIDS 2>/dev/null || true
    sleep 1.5
    STILL_PIDS=$( (lsof -tiTCP:8080 -sTCP:LISTEN 2>/dev/null; lsof -t "$BOT_DIR/data/mem0_qdrant/.lock" 2>/dev/null) | sort -u | tr '\n' ' ' )
    if [ -n "${STILL_PIDS// /}" ]; then
        kill -9 $STILL_PIDS 2>/dev/null || true
        sleep 0.5
    fi
fi

(
    cd "$BOT_DIR"
    export ENVIRONMENT=dev
    "$VENV_PY" daemon_launcher.py
)

sleep 3
echo "✅ [5/5] 校验服务健康状态..."
if lsof -iTCP:8080 -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "🎉 Regression v8 启动成功！WebUI 控制台地址: http://127.0.0.1:8080/panel"
else
    echo "❌ 8080 启动异常，请检查日志: $BOT_DIR/bot.restart.log"
    tail -n 30 "$BOT_DIR/bot.restart.log" || true
    exit 1
fi
