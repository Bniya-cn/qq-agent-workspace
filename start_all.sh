#!/usr/bin/env bash
set -e
ROOT_DIR="/Users/daijinglin/Desktop/qq-agent-workspace"

# 1. 确保 __init__.py 中的聊天语音预算 (45s)、语音模式下生图放行与引用消息多字段回查已同步
"$ROOT_DIR/bot/.venv/bin/python" - << 'PY'
from pathlib import Path

init_path = Path("/Users/daijinglin/Desktop/qq-agent-workspace/bot/src/plugins/agent_core/__init__.py")
if init_path.is_file():
    code = init_path.read_text(encoding="utf-8")
    updated = code.replace("CHAT_TTS_TIMEOUT = 8.0", "CHAT_TTS_TIMEOUT = 45.0")
    updated = updated.replace(
        "if state.voice_enabled and not explicit_formats:",
        "if state.voice_enabled and not explicit_formats and not _is_draw_intent(plain, has_image=bool(image_b64_list)):",
    )
    updated = updated.replace(
        "if reply_id and (not reply_text or (not reply_imgs and not direct_imgs)):",
        "if reply_id and (not reply_text or not reply_imgs):",
    )
    updated = updated.replace(
        "if group_id and (reply_id or mentions_img) and not reply_imgs and not direct_imgs and not reply_text:",
        "if group_id and (reply_id or mentions_img) and (not reply_imgs or not reply_text) and not direct_imgs:",
    )
    updated = updated.replace(
        'hm_id = str(hm.get("message_id") or hm.get("real_id") or "").strip()\n                    raw_m = hm.get("message") or hm.get("raw_message")\n                    hm_imgs = _extract_images_from_segments(raw_m, ignore_stickers=False)\n                    hm_txt, hm_fids = _extract_text_and_forwards_from_segments(raw_m)\n                    if reply_id and hm_id == reply_id:',
        'cand_ids = {str(hm.get(k) or "").strip() for k in ("message_id", "real_id", "message_seq", "id")} - {""}\n                    raw_m = hm.get("message") or hm.get("raw_message")\n                    hm_imgs = _extract_images_from_segments(raw_m, ignore_stickers=False)\n                    hm_txt, hm_fids = _extract_text_and_forwards_from_segments(raw_m)\n                    if reply_id and reply_id in cand_ids:',
    )
    if updated != code:
        init_path.write_text(updated, encoding="utf-8")
PY

echo "🔄 [1/3] 检查 GPT-SoVITS 语音合成服务 (端口 9880)..."
if ! lsof -tiTCP:9880 -sTCP:LISTEN >/dev/null 2>&1; then
    if [ -f "$ROOT_DIR/gpt-sovits/scripts/start_api.sh" ]; then
        bash "$ROOT_DIR/gpt-sovits/scripts/start_api.sh"
    elif [ -f "$ROOT_DIR/gpt-sovits/start_tts.sh" ]; then
        (cd "$ROOT_DIR/gpt-sovits" && nohup ./start_tts.sh > "$ROOT_DIR/gpt-sovits/logs/api.log" 2>&1 &)
    fi
    echo "   -> 已拉起 9880 语音服务"
else
    echo "   -> 9880 语音服务已在线"
fi

echo "🧪 [2/3] 执行源码编译与单元测试校验..."
cd "$ROOT_DIR"
"$ROOT_DIR/bot/.venv/bin/python" -m py_compile bot/src/plugins/agent_core/*.py
ENVIRONMENT=dev "$ROOT_DIR/bot/.venv/bin/python" -m unittest discover -s bot/tests -p 'test_*.py'

echo "🚀 [3/3] 优雅重启 8080 NoneBot 机器人服务..."
OLD_PID=$(lsof -tiTCP:8080 -sTCP:LISTEN || true)
if [ -n "$OLD_PID" ]; then
    kill -TERM $OLD_PID 2>/dev/null || true
    sleep 2
fi
cd "$ROOT_DIR/bot"
"$ROOT_DIR/bot/.venv/bin/python" daemon_launcher.py
sleep 3
curl -s http://127.0.0.1:8080/api/status | python3 -m json.tool
