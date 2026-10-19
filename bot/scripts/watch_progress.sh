#!/usr/bin/env bash
# ==============================================================================
# PRTS // QQ Agent - 四大二游角色全立绘实况下载监控启动器
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BOT_DIR="$WORKSPACE_ROOT/bot"
VENV_PYTHON="$BOT_DIR/.venv/bin/python"

if [ -f "$VENV_PYTHON" ]; then
    PY_BIN="$VENV_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
else
    PY_BIN="python"
fi

exec "$PY_BIN" "$SCRIPT_DIR/monitor_ref_progress.py" "$@"
