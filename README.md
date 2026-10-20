# QQ Agent

> 新用户请先阅读[开始使用与部署说明](GETTING_STARTED.md)：提供手动部署步骤、可复制给 Agent 的部署提示词，以及本地预览图保存位置。

这是 QQ Agent 的个人项目仓库，保存自定义机器人后端、测试、角色资料和 macOS 控制器源码。

## 技术来源

- [NoneBot2](https://github.com/nonebot/nonebot2)：开源 Python 机器人框架；本项目用它运行消息插件。
- [nonebot-adapter-onebot](https://github.com/nonebot/adapter-onebot)：NoneBot2 的 OneBot 适配器。
- [OneBot 11](https://github.com/botuniverse/onebot-11)：NapCat 与后端之间使用的通信协议。
- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)：连接 QQ 并提供 OneBot 接口的第三方程序。
- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)：可选的本地语音合成服务。

`bot/src/plugins/agent_core/` 和 `macos-controller/QQAgentController/` 是本项目的业务集成代码，不是 NoneBot2 或 OneBot 上游代码。NapCat 与 GPT-SoVITS 的本地安装、模型权重和运行数据不放进本仓库；其中 GPT-SoVITS 目录仍由它自己的上游 Git 仓库管理。

## 目录

```text
bot/                  NoneBot2 后端、插件和测试
macos-controller/     SwiftUI 菜单栏控制器源码
README.md             项目来源与启动概要
```

本仓库保留共享角色预设和知识资料。`.env`、QQ 群记录、会话数据库、生成文件、日志、语音模型、NapCat 安装包和构建产物均为本机数据，不提交到 Git。

## 本机启动概要

1. 在 `bot/` 创建 Python 虚拟环境并安装 `requirements.txt`。
2. 复制 `bot/.env.example` 为 `bot/.env`，在本机填写服务密钥和应用配置。
3. 单独安装并启动 QQ + NapCat，将 OneBot V11 反向 WebSocket 指向 `ws://127.0.0.1:8080/onebot/v11/ws`。
4. 从 `bot/` 目录运行 `python bot.py`。
5. 需要语音时，另行安装并运行 GPT-SoVITS API（默认 `127.0.0.1:9880`）。

具体密钥、QQ 登录和本机目录配置只保存在本地，不要提交到仓库。
