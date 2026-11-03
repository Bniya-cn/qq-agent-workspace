#!/bin/bash
# 一键启动微信版阿米娅 (AstrBot + GeweChat)
cd "$(dirname "$0")"

echo "=========================================="
echo "正在启动 微信版阿米娅 (AstrBot + GeweChat)..."
echo "=========================================="

# 检查 docker 命令是否存在
if ! command -v docker &> /dev/null; then
    echo "❌ 错误: 未检测到 docker 命令，请先启动或安装 Docker Desktop！"
    exit 1
fi

# 启动 Docker Compose
docker compose up -d

echo ""
echo "✅ 服务已在后台启动！"
echo "------------------------------------------"
echo "🌐 AstrBot 控制面板: http://127.0.0.1:6185"
echo "💡 提示: 首次进入需设置管理员账密，并在面板扫码登录微信小号。"
echo "=========================================="
