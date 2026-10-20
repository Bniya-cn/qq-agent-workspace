# QQ Agent 开始使用与部署说明

本说明提供两种开始方式：自己按步骤部署，或把文末提示词交给有本机终端权限的 AI Agent 协助。无论选择哪种方式，都先从自己的 GitHub 有权访问的仓库克隆代码；不要把 API 密钥、QQ 聊天记录、机器人登录凭据或模型权重提交到 GitHub。

本仓库保存的是 QQ Agent 的业务代码。NapCat、GPT-SoVITS 运行目录、模型权重和个人运行数据不随仓库提供。仓库当前为 Private；克隆账号需要拥有仓库读取权限。

## 方式一：手动部署（macOS）

### 需要准备

- macOS 电脑，以及 Git、Python 3.12。项目当前维护环境使用 Python 3.12；请用 `python3.12` 创建虚拟环境。
- 一个用于机器人的 QQ 账号，以及单独安装和登录的 NapCatQQ。NapCat 通过 OneBot V11 反向 WebSocket 连接到本项目。
- 根据需要准备 DeepSeek 和阿里云百炼 API 密钥。没有本地 GPT-SoVITS 时，文字聊天仍可运行；语音合成需要另行部署 GPT-SoVITS。

### 1. 从 GitHub 克隆并安装依赖

在终端执行：

```bash
git clone https://github.com/Bniya-cn/qq-agent-workspace.git
cd qq-agent-workspace/bot
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果提示 `python3.12` 不存在，请先安装 Python 3.12，再重试；不要复用其他项目的虚拟环境。

### 2. 配置本机环境变量

```bash
cp .env.example .env.dev
```

编辑 `bot/.env.dev`。本机与 NapCat 在同一台电脑时，可按以下方式填写；密钥仅保存在本机：

```dotenv
ENVIRONMENT=dev
DRIVER=~fastapi
HOST=127.0.0.1
PORT=8080
SUPERUSER_QQ=你的管理员QQ号
ONEBOT_ACCESS_TOKEN=与NapCat配置相同的随机令牌
DEEPSEEK_API_KEY=你的DeepSeek密钥
DASHSCOPE_API_KEY=你的百炼密钥
```

密钥和选填模型/应用配置的说明见 [`bot/.env.example`](bot/.env.example)。若给 NapCat 配置了 Access Token，两侧必须完全一致；若不使用 Token，两侧都留空。`HOST=127.0.0.1` 只允许本机访问控制面板，适合 NapCat 和后端同机运行的场景。不要为了远程访问而直接把无鉴权控制面板暴露到公网。

### 3. 启动后端

在 `bot/` 目录、且已激活 `.venv` 的终端中运行：

```bash
ENVIRONMENT=dev .venv/bin/python bot.py
```

保持该终端运行。看到 NoneBot 启动完成后，在同一台电脑的浏览器打开：

- 控制面板：<http://127.0.0.1:8080/panel>
- 状态接口：<http://127.0.0.1:8080/api/status>

后端单独启动成功只证明本地服务已运行；机器人尚未连接时，状态接口可能显示未连接。

### 4. 配置 NapCat 连接 QQ

在 NapCat 的 OneBot V11 网络配置中新增**反向 WebSocket**，并使用：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

把 NapCat 登录到机器人 QQ 账号，并核对 Access Token 与 `bot/.env.dev` 一致。确认 NapCat 日志显示连接成功后，再检查控制面板的机器人连接状态。群聊响应还需要在控制面板中启用机器人，并配置允许响应的群。

### 5. 可选服务

- **语音**：单独安装和启动 GPT-SoVITS，并确保 `TTS_URL` 指向可访问的本机服务（默认 `http://127.0.0.1:9880/tts`）。仓库不包含 GPT-SoVITS 程序和模型权重。
- **macOS 菜单栏控制端**：需要时再按 [`macos-controller/README.md`](macos-controller/README.md) 的说明构建。它不是启动 NoneBot 后端的前置条件。

当前根目录的 `start_all.sh` 含本机绝对路径，并会执行额外的升级、同步、测试和进程操作；它不是面向新机器的通用安装器。首次部署请按本页手动步骤操作，并先阅读脚本再决定是否在自己的机器上使用。

## 方式二：复制提示词交给你的 Agent

将下面整段提示词复制给能够访问你本机终端与项目目录的 AI Agent。使用前把占位路径换成你的克隆目录；不要把真实密钥粘贴进聊天提示词。

```text
请协助我在这台 macOS 电脑上部署 GitHub 仓库 Bniya-cn/qq-agent-workspace。

项目路径：/把这里替换为本机的/qq-agent-workspace

请先读取仓库 README.md、GETTING_STARTED.md、bot/.env.example 和当前 Git 状态，再按 GETTING_STARTED.md 的“手动部署”步骤检查环境、创建项目自己的 Python 3.12 虚拟环境并安装依赖。请保留已有文件和未提交改动，不要清理或重置工作区。

请勿直接运行 start_all.sh；它包含特定电脑的绝对路径及额外进程操作。请勿打印、读取后展示、提交或上传 .env 中的密钥；如果缺少密钥，请告诉我应在本机 bot/.env.dev 的哪个字段填写，不要要求我把密钥发到聊天里。请勿向 QQ 群或用户发送测试消息，也不要登录或操作我的 QQ 账号。

配置 NapCat 时只给出反向 WebSocket 地址 ws://127.0.0.1:8080/onebot/v11/ws，并提醒我核对同机部署和 Token；不要代替我登录 QQ。完成后运行本地必要检查，打开/探测 /api/status 与 /panel，并区分“后端启动成功”“NapCat 已连接”和“QQ 客户端实际收发成功”这几种证据。没有真实 QQ 收发证据时，不要声称端到端部署完成。

如果要把生成的预览图留在本机，请存放到项目目录 bot/data/previews/（不存在时可创建）；该目录属于本地运行数据，不要提交到 GitHub。
```

## 本地预览图保存位置

如果要把生成的图片预览留在本机，可保存到：

```text
<克隆目录>/bot/data/previews/
```

例如仓库克隆在 `~/Desktop/qq-agent-workspace` 时，对应目录为 `~/Desktop/qq-agent-workspace/bot/data/previews/`。目录不存在时先创建：

```bash
mkdir -p bot/data/previews
```

`bot/data/` 是本机运行数据目录，已由 `.gitignore` 排除；预览图不会随普通 Git 提交上传。请勿将私聊截图、群聊内容或带个人信息的图片放入公开仓库的 `assets/`。

## 更新与排错

- 更新代码前先看 `git status`，确认并保存自己的未提交工作；不要用 `git reset --hard`、`git clean` 或覆盖式恢复命令。
- `8080` 无法启动时，先检查端口占用和 `bot/bot.restart.log`，不要批量结束 Python 进程。
- 页面能打开但机器人未连接时，检查 NapCat 是否在线、反向 WebSocket 地址和 Token 是否匹配。
- 文字模型不可用时，检查 `.env.dev` 中对应密钥是否已由你在本机填写。
- 图片、语音和文件的 QQ 实际送达情况需要在目标 QQ 客户端验证；本地 HTTP 接口成功不能代替 QQ 收件证明。
