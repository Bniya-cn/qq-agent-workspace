# 微信版阿米娅（AstrBot + GeweChat 独立部署）

本项目是基于 **AstrBot** + **GeweChat (iPad 微信协议)** 搭建的**微信端独立阿米娅（Amiya）**机器人。

> ⚠️ **隔离原则**：
> 本系统与工作区根目录的 QQ 机器人（NoneBot2）**完全独立**，不共享端口、不共享容器，启动与停止微信端**绝不影响原版 QQ 阿米娅**。

---

## 目录结构

```text
astrbot-wechat/
├── docker-compose.yml           # 容器编排 (GeweChat: 2531, AstrBot: 6185)
├── start_wechat.sh             # 一键启动脚本
├── stop_wechat.sh              # 一键停止脚本
├── amiya_persona/
│   ├── amiya_prompt.txt        # 微信版专属阿米娅系统提示词 (Prompt)
│   └── amiya_handbook.txt      # 阿米娅与罗德岛知识库档案
└── README.md                   # 部署与配置指引
```

---

## 快速使用指南

### 第一步：一键启动容器
确保你的电脑已开启 **Docker Desktop**，然后在终端运行：

```bash
cd /Users/daijinglin/Desktop/qq-agent-workspace/astrbot-wechat
bash start_wechat.sh
```

---

### 第二步：登录 AstrBot 控制面板
1. 在浏览器打开：`http://127.0.0.1:6185`
2. **首次登录**：系统会提示你初始化管理员账号和密码（请牢记你的密码）。

---

### 第三步：配置大模型（LLM）
1. 在 AstrBot 左侧菜单点击 **服务提供商（LLM）**。
2. 添加提供商，选择 **DeepSeek** 或 **OpenAI 兼容接口**：
   * **API Key**：填入你现有的 DeepSeek API Key（或阿里百炼/通义千问 Key）。
   * **Base URL**：
     * 如果使用 DeepSeek：`https://api.deepseek.com`
     * 如果使用阿里百炼：`https://dashscope.aliyuncs.com/compatible-mode/v1`
   * **模型名称**：如 `deepseek-chat` 或 `qwen-plus`。
3. 点击保存并设为默认模型。

---

### 第四步：导入阿米娅专属人设
1. 在 AstrBot 左侧菜单点击 **人设 / 提示词（Persona）**。
2. 打开本地文件：[`astrbot-wechat/amiya_persona/amiya_prompt.txt`](amiya_persona/amiya_prompt.txt)。
3. 将文件里的全部内容复制并粘贴到 AstrBot 的**系统提示词（System Prompt）**输入框中。
4. （可选）在**知识库管理**中上传 [`astrbot-wechat/amiya_persona/amiya_handbook.txt`](amiya_persona/amiya_handbook.txt)，让微信阿米娅掌握完整的罗德岛背景故事。

---

### 第五步：接入微信（GeweChat 扫码登录）
1. 在 AstrBot 左侧菜单点击 **消息平台（适配器）** $\to$ 添加平台。
2. 平台类型选择 **GeweChat**（或微信相关适配器）：
   * **接口地址**：填入 `http://gewe:2531/v2/api`（容器内互联）或 `http://127.0.0.1:2531/v2/api`
   * **下载/静态地址**：填入 `http://gewe:2532/download`
   * **Token**：留空或根据面板指引自动生成
3. 保存后点击 **启动/连接**，面板将生成并显示**登录二维码**。
4. 拿出你的**微信小号**，打开微信扫一扫，确认在 iPad 端登录。
5. 登录成功后，你的微信小号就正式变成阿米娅啦！

---

## 🛡️ 微信防风控白名单设置（极重要）

为了保护你的微信小号不被腾讯风控踢下线或临时限制，请务必在 AstrBot 面板做好限制：

1. **白名单模式**：
   * 进入 AstrBot **设置 / 权限管理**；
   * 将响应模式设置为**仅响应指定好友**；
   * 填入你自己的微信大号 ID / 备注名，先不要开放陌生人无限制私聊。
2. **打字拟人延迟**：
   * 在设置中开启“打字延迟”（建议 2~4 秒），让回复体验更像微信真实好友在打字，避免被微信算法判定为高频外挂。
3. **日常维护**：
   * 手机微信平时保持登录该小号，偶尔在手机端浏览、点赞朋友圈，维持账号真实权重。
