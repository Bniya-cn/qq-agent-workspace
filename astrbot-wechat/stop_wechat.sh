#!/bin/bash
# 一键停止微信版阿米娅 (AstrBot + GeweChat)
cd "$(dirname "$0")"

echo "正在停止 微信版阿米娅 容器..."
docker compose down

echo "✅ 微信版已停止！原 QQ 版 Amiya 保持运行中。"
