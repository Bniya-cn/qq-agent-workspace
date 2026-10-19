#!/usr/bin/env python3
"""NoneBot2 后台守护拉起器（双 fork + setsid 脱离终端与 App 父进程）。"""
import os
import sys

# 1) 第一层 fork，父进程立即退出给调用方释放控制
if os.fork() > 0:
    sys.exit(0)

# 2) 创建新的独立会话，脱离原有控制终端与进程组
os.setsid()

# 3) 第二层 fork，防止进程重新获得控制终端
if os.fork() > 0:
    sys.exit(0)

bot_dir = "/Users/daijinglin/Desktop/qq-agent-workspace/bot"
os.chdir(bot_dir)

python_bin = os.path.join(bot_dir, ".venv/bin/python")
bot_script = os.path.join(bot_dir, "bot.py")
log_file = os.path.join(bot_dir, "bot.restart.log")

# 4) 为脱离终端的解释器提供有效 stdin，避免重启后初始化标准流失败。
stdin_fd = os.open(os.devnull, os.O_RDONLY)
os.dup2(stdin_fd, 0, inheritable=True)
if stdin_fd != 0:
    os.close(stdin_fd)

# 重定向 stdout 与 stderr 到日志文件
with open(log_file, "a", encoding="utf-8") as f:
    f.write(f"\n[DAEMON] Starting bot.py via daemon launcher at PID {os.getpid()}\n")
    f.flush()
    os.dup2(f.fileno(), 1)
    os.dup2(f.fileno(), 2)

# 5) 注入环境变量以确保加载 dev 配置和 FastAPI controller_api 路由
os.environ["ENVIRONMENT"] = "dev"

# 控制台先拉起守护进程再退出旧后端；等待端口释放，避免新进程抢先绑定失败。
import socket
import time

for _ in range(80):
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8080)) != 0:
            break
    time.sleep(0.1)
else:
    raise RuntimeError("旧后端未释放 8080，取消重复启动")

# 6) 变身为 Python 解释器执行 bot.py
os.execv(python_bin, [python_bin, bot_script])
