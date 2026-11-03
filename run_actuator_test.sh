#!/usr/bin/env bash
set -e
ROOT_DIR="/Users/daijinglin/Desktop/qq-agent-workspace"
VENV_PY="$ROOT_DIR/bot/.venv/bin/python"
export PYTHONPATH="$ROOT_DIR/bot"

echo "🔍 [1/2] 编译检查 device_actuator 及关联模块..."
"$VENV_PY" -m py_compile \
  "$ROOT_DIR/bot/src/plugins/agent_core/device_actuator.py" \
  "$ROOT_DIR/bot/src/plugins/agent_core/autonomous_agent.py" \
  "$ROOT_DIR/bot/src/plugins/agent_core/controller_api.py" \
  "$ROOT_DIR/bot/tests/test_device_actuator_v11.py"

echo "🚀 [2/2] 运行方案 A (Mac 原生通知关怀) 与方案 B (MuMu 模拟器视觉定位) 验证测试..."
"$VENV_PY" -m unittest -v "$ROOT_DIR/bot/tests/test_device_actuator_v11.py"
