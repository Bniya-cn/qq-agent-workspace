"""QQ Agent 双模式控制台前端（严格遵循 @design-taste-frontend 规范）。
将「🤖 机器人模式（Bot Agent Mode）」与「🎙️ 语音模式（Voice & SoVITS Mode）」解耦为两个独立工作区。
"""
from __future__ import annotations

import asyncio
from nonebot import logger
from . import ref_fetcher

class _DynamicPanelHtml(str):
    def encode(self, *args, **kwargs) -> bytes:
        return super().encode(*args, **kwargs)

    def __str__(self) -> str:
        return str(super().__str__())

_RAW_PANEL_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PRTS // QQ Agent 双模控制中枢</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['"Plus Jakarta Sans"', '-apple-system', 'BlinkMacSystemFont', '"PingFang SC"', 'sans-serif'],
            mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
          },
          colors: {
            surface: {
              950: '#090A0F',
              900: '#10121A',
              850: '#151822',
              800: '#1C202E',
              700: '#262B3D',
            }
          }
        }
      }
    }
  </script>
  <style>
    :root {
      --mode-accent: #0EA5E9;
      --mode-accent-soft: rgba(14, 165, 233, 0.12);
      --mode-accent-border: rgba(14, 165, 233, 0.35);
    }
    body[data-workspace="voice"] {
      --mode-accent: #10B981;
      --mode-accent-soft: rgba(16, 185, 129, 0.12);
      --mode-accent-border: rgba(16, 185, 129, 0.35);
    }
    body {
      background-color: #090A0F;
      color: #F1F5F9;
      background-image:
        radial-gradient(circle at 12% 0%, var(--mode-accent-soft), transparent 38%),
        linear-gradient(180deg, rgba(255,255,255,0.015) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.015) 1px, transparent 1px);
      background-size: 100% 100%, 32px 32px, 32px 32px;
      transition: background-image 0.25s ease;
    }
    .tactile-btn {
      transition: transform 0.12s cubic-bezier(0.2, 0, 0, 1), background-color 0.15s ease, border-color 0.15s ease;
    }
    .tactile-btn:active:not(:disabled) {
      transform: scale(0.98) translateY(1px);
    }
    .accent-border { border-color: var(--mode-accent-border); }
    .accent-bg { background-color: var(--mode-accent); }
    .accent-text { color: var(--mode-accent); }
    .accent-pill {
      background-color: var(--mode-accent-soft);
      border: 1px solid var(--mode-accent-border);
      color: var(--mode-accent);
    }
    input[type="range"] {
      accent-color: var(--mode-accent);
    }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #090A0F; }
    ::-webkit-scrollbar-thumb { background: #262B3D; border-radius: 3px; }
  </style>
</head>
<body data-workspace="bot" class="min-h-screen antialiased selection:bg-sky-500/30 selection:text-sky-200">

  <!-- 顶部单行导航栏 (Height: 68px, 遵守 @design-taste-frontend 单行导航与模式切换准则) -->
  <header class="sticky top-0 z-30 h-[68px] border-b border-surface-700/80 bg-surface-950/90 backdrop-blur-md px-6 flex items-center justify-between">
    <div class="flex items-center gap-4">
      <div class="w-9 h-9 rounded-xl accent-pill flex items-center justify-center font-mono font-semibold text-sm">
        PRTS
      </div>
      <div>
        <div class="flex items-center gap-2.5">
          <h1 class="text-base font-bold tracking-tight text-slate-100">QQ Agent 战术控制中枢</h1>
          <span id="header-conn-badge" class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-mono bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>
            8080 ONLINE
          </span>
        </div>
        <p class="text-xs text-slate-400 font-mono" id="header-sub-status">Bot: 1412773476 · 直连百炼与 GPT-SoVITS</p>
      </div>
    </div>

    <!-- 核心双模式切换器：严格区分「机器人模式」与「语音模式」 -->
    <div class="flex items-center bg-surface-900 p-1 rounded-xl border border-surface-700">
      <button id="tab-btn-bot" onclick="switchWorkspace('bot')" class="tactile-btn px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 bg-sky-500 text-slate-950 shadow-sm">
        <span>🤖 机器人模式</span>
        <span class="font-mono text-[10px] opacity-80">AGENT</span>
      </button>
      <button id="tab-btn-voice" onclick="switchWorkspace('voice')" class="tactile-btn px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 text-slate-400 hover:text-slate-200">
        <span>🎙️ 语音模式</span>
        <span id="voice-tab-dot" class="w-2 h-2 rounded-full bg-slate-600"></span>
      </button>
    </div>

    <!-- 右侧快捷动作 -->
    <div class="flex items-center gap-2.5">
      <button id="btn-toggle-expand-all" onclick="toggleAllBoxesExpand()" class="tactile-btn px-3 py-1.5 rounded-lg text-xs font-medium border border-sky-500/40 bg-sky-500/10 hover:bg-sky-500/20 text-sky-300">
        ↕️ 一键撑开/恢复高度
      </button>
      <button onclick="refreshDashboard()" class="tactile-btn px-3 py-1.5 rounded-lg text-xs font-mono border border-surface-700 bg-surface-900 hover:bg-surface-800 text-slate-300">
        ↻ 刷新状态
      </button>
      <button onclick="restartBackend()" class="tactile-btn px-3 py-1.5 rounded-lg text-xs font-medium border border-amber-500/30 bg-amber-500/10 hover:bg-amber-500/20 text-amber-300">
        热重启 8080
      </button>
    </div>
  </header>

  <!-- 全局 Toast 通知 -->
  <div id="toast-box" class="fixed bottom-5 right-5 z-50 hidden px-4 py-2.5 rounded-xl border border-surface-700 bg-surface-900/95 text-xs font-medium text-slate-100 shadow-xl"></div>

  <main class="max-w-[1360px] mx-auto px-6 py-6 space-y-6">

    <!-- =================================================================== -->
    <!-- ⚡ 实时生成进度监控台（文字生成 / 二次元生图 4阶段 / 语音合成 3阶段）   -->
    <!-- =================================================================== -->
    <section id="rt-progress-panel" class="rounded-2xl border border-sky-500/30 bg-surface-900/95 p-5 shadow-lg">
      <div class="flex flex-wrap items-center justify-between gap-3 pb-3.5 mb-4 border-b border-surface-700/80">
        <div class="flex items-center gap-3">
          <span class="px-2.5 py-1 rounded-lg text-xs font-mono font-bold bg-sky-500/15 text-sky-300 border border-sky-500/30">LIVE PIPELINE</span>
          <h2 class="text-base font-bold tracking-tight text-slate-100">⚡ 实时生成进度监控台</h2>
          <span id="rt-poll-indicator" class="text-xs font-mono text-slate-400">1.2s 极速轮询中 · 所有框体右下角均可拖拽放大并自动记忆高度</span>
        </div>
        <div class="flex items-center gap-2">
          <button onclick="toggleAllBoxesExpand()" class="tactile-btn px-3 py-1 rounded-lg text-xs font-mono border border-sky-500/30 bg-sky-500/10 hover:bg-sky-500/20 text-sky-300">
            ↕️ 一键撑开/恢复高度
          </button>
        </div>
      </div>

      <div id="rt-cards-container" class="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <!-- 默认展示三链路卡片骨架，由 1.2s 轮询实时填充最新状态 -->
      </div>
    </section>

    <!-- =================================================================== -->
    <!-- 工作区 1：🤖 机器人模式 (Bot Agent Workspace)                        -->
    <!-- =================================================================== -->
    <section id="workspace-bot" class="space-y-6">

      <!-- 顶部 Bento 状态与主控横幅 (Asymmetric 3-Cell Grid) -->
      <div class="grid grid-cols-1 lg:grid-cols-12 gap-4">
        <!-- Cell 1: 机器人总开关与连接态 (5 cols) -->
        <div class="lg:col-span-5 rounded-2xl border border-surface-700 bg-surface-900/90 p-5 flex flex-col justify-between">
          <div class="flex items-start justify-between">
            <div>
              <span class="font-mono text-[11px] uppercase tracking-wider text-sky-400">BOT MASTER SWITCH</span>
              <h2 class="text-xl font-bold tracking-tight mt-1">群聊与私聊响应总闸</h2>
              <p class="text-xs text-slate-400 mt-1">关闭后机器人在所有群保持静默，仅响应管理员私聊指令。</p>
            </div>
            <label class="relative inline-flex items-center cursor-pointer mt-1">
              <input type="checkbox" id="bot-global-toggle" class="sr-only peer" onchange="toggleGlobal(this.checked)">
              <div class="w-12 h-6 bg-surface-700 rounded-full peer peer-checked:bg-sky-500 after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:after:translate-x-6"></div>
            </label>
          </div>
          <div class="mt-5 pt-4 border-t border-surface-800 flex items-center justify-between text-xs font-mono">
            <span class="text-slate-400">OneBot V11 反向 WS</span>
            <span id="bot-ws-status" class="text-emerald-400">ws://127.0.0.1:8080/onebot/v11/ws</span>
          </div>
        </div>

        <!-- Cell 2: 官方干员人设与群画像模式切换 (4 cols) -->
        <div class="lg:col-span-4 rounded-2xl border border-surface-700 bg-surface-900/90 p-5 flex flex-col justify-between">
          <div>
            <div class="flex items-center justify-between">
              <h2 class="text-base font-bold text-slate-100">当前激活人格模式</h2>
              <span id="active-char-badge" class="px-2 py-0.5 rounded-md font-mono text-xs bg-sky-500/15 text-sky-400 border border-sky-500/30">amiya</span>
            </div>
            <p class="text-xs text-slate-400 mt-1">优先级：安慰模式 &gt; 干员模式 &gt; 群画像数字人格</p>
            <div class="grid grid-cols-2 gap-2 mt-3.5">
              <button onclick="setCharacter('amiya')" data-char="amiya" class="char-btn tactile-btn px-3 py-2 rounded-xl border border-surface-700 bg-surface-850 text-left">
                <div class="text-xs font-semibold text-slate-100">阿米娅 Amiya</div>
                <div class="text-[11px] text-slate-400">百炼 RAG + 温柔领袖</div>
              </button>
              <button onclick="setCharacter('kaltsit')" data-char="kaltsit" class="char-btn tactile-btn px-3 py-2 rounded-xl border border-surface-700 bg-surface-850 text-left">
                <div class="text-xs font-semibold text-slate-100">凯尔希 Kal'tsit</div>
                <div class="text-[11px] text-slate-400">百炼 RAG + 冷静医疗</div>
              </button>
              <button onclick="setCharacter('dania')" data-char="dania" class="char-btn tactile-btn px-3 py-2 rounded-xl border border-surface-700 bg-surface-850 text-left">
                <div class="text-xs font-semibold text-slate-100">达妮娅 Dania</div>
                <div class="text-[11px] text-slate-400">鸣潮共鸣者 + 傲娇战友</div>
              </button>
              <button onclick="setCharacter('none')" data-char="none" class="char-btn tactile-btn px-3 py-2 rounded-xl border border-surface-700 bg-surface-850 text-left">
                <div class="text-xs font-semibold text-slate-100">群画像复刻模式</div>
                <div class="text-[11px] text-slate-400">按群 JSON 模仿群友</div>
              </button>
            </div>

            <!-- 💬 角色日常闲聊模式与风格开关 -->
            <div class="mt-3.5 pt-3 border-t border-surface-800 space-y-2.5">
              <div class="flex items-center justify-between">
                <div>
                  <div class="flex items-center gap-2">
                    <span class="text-xs font-bold text-sky-400">💬 极速日常口语闲聊</span>
                    <span id="small-talk-status-badge" class="px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-400">OFF</span>
                  </div>
                  <p class="text-[11px] text-slate-400 mt-0.5">告别百炼长篇公文小作文，DeepSeek 极速短句响应</p>
                </div>
                <label class="relative inline-flex items-center cursor-pointer">
                  <input type="checkbox" id="small-talk-toggle" class="sr-only peer" onchange="toggleSmallTalk(this.checked)">
                  <div class="w-10 h-5 bg-surface-700 rounded-full peer peer-checked:bg-sky-500 after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-5"></div>
                </label>
              </div>

              <!-- 闲聊风格二级切换开关 -->
              <div class="bg-surface-950/70 p-2.5 rounded-xl border border-surface-800/80 flex items-center justify-between text-xs">
                <div>
                  <div class="text-[11px] text-slate-400">闲聊风格定制：</div>
                  <div id="small-talk-style-desc" class="text-xs font-semibold text-emerald-400 mt-0.5">🌸 纯角色设定 (5~20字)</div>
                </div>
                <div class="flex items-center gap-1 bg-surface-800/90 p-1 rounded-lg border border-surface-700">
                  <button type="button" id="btn-style-pure" onclick="setSmallTalkStyle('pure_persona')" class="px-2.5 py-1 rounded text-[11px] font-medium transition-all bg-sky-500/20 text-sky-300 border border-sky-500/40">
                    🌸 纯角色 (5~20字)
                  </button>
                  <button type="button" id="btn-style-group" onclick="setSmallTalkStyle('group_mimic')" class="px-2.5 py-1 rounded text-[11px] font-medium transition-all text-slate-400 hover:text-slate-200">
                    🐟 群友接梗 (大肥鱼)
                  </button>
                </div>
              </div>
            </div>

            <!-- ⚡ 默认回复模式 (免触发词 / 3s防刷屏) -->
            <div class="mt-2.5 pt-2.5 border-t border-surface-800 flex items-center justify-between">
              <div>
                <div class="flex items-center gap-2">
                  <span class="text-xs font-bold text-amber-400">⚡ 默认回复模式</span>
                  <span id="auto-reply-status-badge" class="px-2 py-0.5 rounded text-[10px] font-mono bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">ON · 免触发词</span>
                </div>
                <p class="text-[11px] text-slate-400 mt-0.5">免 @ / 免角色名自动搭话（内置 3s 冷却严格防刷屏）</p>
              </div>
              <label class="relative inline-flex items-center cursor-pointer">
                <input type="checkbox" id="auto-reply-toggle" class="sr-only peer" checked onchange="toggleAutoReply(this.checked)">
                <div class="w-10 h-5 bg-surface-700 rounded-full peer peer-checked:bg-amber-500 after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-5"></div>
              </label>
            </div>
          </div>
        </div>

        <!-- Cell 3: 千问生图与百炼提示词 Agent 链路状态 (3 cols) -->
        <div class="lg:col-span-3 rounded-2xl border border-surface-700 bg-surface-900/90 p-5 flex flex-col justify-between">
          <div>
            <h2 class="text-base font-bold text-slate-100">二次元生图与工作流管线</h2>
            <div class="mt-3 space-y-2 text-xs font-mono">
              <div class="flex justify-between">
                <span class="text-slate-400">主渲染引擎</span>
                <span id="info-img-model" class="text-sky-400">qwen-image-2.1-pro</span>
              </div>
              <div class="flex justify-between">
                <span class="text-slate-400">提示词 Agent</span>
                <span id="info-prompt-agent" class="text-emerald-400">97fcb1dc...e6d0</span>
              </div>
              <div class="flex justify-between">
                <span class="text-slate-400">视觉解析器</span>
                <span class="text-slate-200">qwen-vl-max</span>
              </div>
              <div class="flex justify-between">
                <span class="text-slate-400">已归档角色立绘</span>
                <span id="info-refs-count" class="text-slate-200">3 张</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 中部两列业务控制区：白名单群管理 + 安慰模式 + 群聊天记录画像提取 -->
      <div class="grid grid-cols-1 lg:grid-cols-12 gap-4">
        <!-- 左侧：群白名单与安慰目标管理 (6 cols) -->
        <div class="lg:col-span-6 rounded-2xl border border-surface-700 bg-surface-900/90 p-5 space-y-5">
          <!-- 群白名单管理 -->
          <div>
            <div class="flex items-center justify-between">
              <div>
                <h3 class="text-sm font-bold text-slate-100">群聊白名单管理 (active_groups)</h3>
                <p class="text-xs text-slate-400">仅在以下白名单群中响应 @机器人 消息，支持直接增删群号。</p>
              </div>
              <span id="group-count-badge" class="font-mono text-xs text-slate-400">0 个群</span>
            </div>
            <div class="flex gap-2 mt-3">
              <input id="input-group-id" type="text" placeholder="输入 QQ 群号（如 130439695）" class="flex-1 px-3 py-1.5 rounded-xl bg-surface-950 border border-surface-700 text-xs font-mono text-slate-100 focus:outline-none focus:border-sky-500" />
              <button onclick="modifyGroup('add')" class="tactile-btn px-4 py-1.5 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 font-semibold text-xs">
                + 加入白名单
              </button>
            </div>
            <div id="active-groups-list" class="flex flex-wrap gap-2 mt-3"></div>
          </div>

          <div class="border-t border-surface-800 pt-4">
            <div class="flex items-center justify-between">
              <div>
                <h3 class="text-sm font-bold text-slate-100">定向情绪安慰模式 (comfort_targets)</h3>
                <p class="text-xs text-slate-400">绑定的 QQ 号在非生图对话时优先走温柔共情安慰通道，点击 × 可解绑。</p>
              </div>
            </div>
            <div class="flex gap-2 mt-3">
              <input id="input-comfort-qq" type="text" placeholder="输入需要安慰的 QQ 号" class="flex-1 px-3 py-1.5 rounded-xl bg-surface-950 border border-surface-700 text-xs font-mono text-slate-100 focus:outline-none focus:border-sky-500" />
              <button onclick="modifyComfort(true)" class="tactile-btn px-4 py-1.5 rounded-xl border border-sky-500/40 bg-sky-500/15 hover:bg-sky-500/25 text-sky-300 font-semibold text-xs">
                + 绑定安慰
              </button>
            </div>
            <div id="comfort-targets-list" class="flex flex-wrap gap-2 mt-3"></div>
          </div>
        </div>

        <!-- 右侧：群画像数字人格提取 + 百炼工作流快捷测试 (6 cols) -->
        <div class="lg:col-span-6 rounded-2xl border border-surface-700 bg-surface-900/90 p-5 flex flex-col justify-between space-y-4">
          <div>
            <div class="flex items-center justify-between">
              <div>
                <h3 class="text-sm font-bold text-slate-100">群聊天记录数字人格提取 (Task5)</h3>
                <p class="text-xs text-slate-400">填入群号并粘贴聊天记录（或拖入 .txt 文件），自动清洗 CQ 码并提炼该群专属说话风格与 3 组 Few-Shot 示例。</p>
              </div>
              <span id="extracted-groups-pills" class="font-mono text-[11px] text-sky-400"></span>
            </div>
            <div class="grid grid-cols-3 gap-2 mt-3">
              <input id="extract-group-id" type="text" placeholder="目标群号 (如 711676140)" class="col-span-1 px-3 py-1.5 rounded-xl bg-surface-950 border border-surface-700 text-xs font-mono text-slate-100 focus:outline-none focus:border-sky-500" />
              <label class="col-span-2 flex items-center justify-center px-3 py-1.5 rounded-xl border border-dashed border-surface-700 bg-surface-950 hover:border-sky-500/50 cursor-pointer text-xs text-slate-400">
                <span>📂 选择聊天记录 .txt 文件自动填入</span>
                <input type="file" accept=".txt,.log" class="hidden" onchange="loadTxtFile(this)" />
              </label>
            </div>
            <textarea id="extract-raw-text" rows="4" placeholder="在此粘贴群聊天记录文本，或点击上方选择 .txt 文件..." class="w-full mt-2.5 p-3 rounded-xl bg-surface-950 border border-surface-700 text-xs font-mono text-slate-200 focus:outline-none focus:border-sky-500"></textarea>
          </div>
          <div class="flex items-center justify-between">
            <span id="extract-status-msg" class="text-xs font-mono text-slate-400">就绪</span>
            <button onclick="submitGroupExtract()" id="btn-extract-group" class="tactile-btn px-4 py-2 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 font-semibold text-xs">
              提炼并保存群画像 JSON
            </button>
          </div>
        </div>
      </div>

      <!-- 全新核心卡片：群聊成员 (群u) 白名单多选管理中心 -->
      <div class="rounded-2xl border border-sky-500/30 bg-surface-900/90 p-5 space-y-4 shadow-lg">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between pb-3 border-b border-surface-800 gap-3">
          <div>
            <div class="flex items-center gap-2">
              <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-sky-500/15 text-sky-300 border border-sky-500/30">WHITELIST</span>
              <h3 class="text-sm font-bold text-slate-100">🛡️ 群聊成员白名单管理 (群u发言限制)</h3>
            </div>
            <p class="text-xs text-slate-400 mt-1">
              开启后，机器人仅响应已勾选群成员的发言（超级管理员自动豁免）。未开启时，白名单群内的所有成员均可正常与机器人对话。
            </p>
          </div>
          <div class="flex items-center gap-3">
            <span id="group-user-wl-status-label" class="text-xs font-mono text-slate-400">白名单过滤: 禁用</span>
            <label class="relative inline-flex items-center cursor-pointer">
              <input type="checkbox" id="group-user-wl-master-toggle" class="sr-only peer" onchange="toggleGroupUserWhitelist(this.checked)">
              <div class="w-11 h-6 bg-surface-700 rounded-full peer peer-checked:bg-sky-500 after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:after:translate-x-5"></div>
            </label>
          </div>
        </div>

        <!-- 控制工具栏：选择群、读取成员、搜索过滤、快捷选择、保存按钮 -->
        <div class="flex flex-wrap items-center justify-between gap-3 bg-surface-950 p-3 rounded-xl border border-surface-800">
          <div class="flex flex-wrap items-center gap-2.5 flex-1 min-w-[280px]">
            <span class="text-xs text-slate-400 font-medium">目标群号:</span>
            <select id="wl-group-select" class="px-3 py-1.5 rounded-xl bg-surface-900 border border-surface-700 text-xs font-mono text-slate-200 focus:outline-none focus:border-sky-500" onchange="onWhitelistGroupChange()">
              <option value="">-- 选择白名单群 --</option>
            </select>
            <input id="wl-custom-group-input" type="text" placeholder="或输入群号" class="w-32 px-3 py-1.5 rounded-xl bg-surface-900 border border-surface-700 text-xs font-mono text-slate-200 focus:outline-none focus:border-sky-500" />
            <button onclick="fetchGroupMembers()" id="btn-fetch-members" class="tactile-btn px-3.5 py-1.5 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 font-semibold text-xs flex items-center gap-1.5">
              <span>🔄 读取群成员</span>
            </button>
          </div>

          <div class="flex flex-wrap items-center gap-2">
            <input id="wl-member-search" type="text" placeholder="🔍 搜索昵称/名片/QQ号..." oninput="filterGroupMembers()" class="w-44 px-3 py-1.5 rounded-xl bg-surface-900 border border-surface-700 text-xs text-slate-200 focus:outline-none focus:border-sky-500" />
            <button onclick="selectAllMembers(true)" class="px-2.5 py-1.5 rounded-lg border border-surface-700 bg-surface-850 hover:border-surface-600 text-slate-300 text-xs">全选</button>
            <button onclick="selectAllMembers(false)" class="px-2.5 py-1.5 rounded-lg border border-surface-700 bg-surface-850 hover:border-surface-600 text-slate-300 text-xs">清空</button>
            <button onclick="invertMemberSelection()" class="px-2.5 py-1.5 rounded-lg border border-surface-700 bg-surface-850 hover:border-surface-600 text-slate-300 text-xs">反选</button>
            <button onclick="saveGroupUserWhitelist()" id="btn-save-user-wl" class="tactile-btn px-4 py-1.5 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs flex items-center gap-1.5">
              <span>💾 保存该群白名单</span>
            </button>
          </div>
        </div>

        <!-- 统计信息与多选容器 -->
        <div class="flex items-center justify-between text-xs px-1">
          <span id="wl-member-stats" class="text-slate-400 font-mono">请先选择群号并点击“读取群成员”</span>
          <span id="wl-save-status-msg" class="text-emerald-400 font-mono text-[11px]"></span>
        </div>

        <div id="wl-members-grid" class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2.5 max-h-[360px] overflow-y-auto p-3 rounded-xl bg-surface-950 border border-surface-800">
          <div class="col-span-full text-center py-10 text-xs text-slate-500">
            暂未加载群成员，请在上方选择群号后点击【🔄 读取群成员】
          </div>
        </div>
      </div>

      <!-- 全新核心卡片：群聊风格实时训练中心 (Training Hub) 与默认 QQ 实机对话测试 -->
      <div class="grid grid-cols-1 lg:grid-cols-12 gap-4">
        <!-- 左侧 7 cols: 群聊风格训练中心 -->
        <div class="lg:col-span-7 rounded-2xl border border-sky-500/30 bg-surface-900/90 p-5 space-y-4 shadow-lg">
          <div class="flex items-center justify-between pb-3 border-b border-surface-800">
            <div>
              <div class="flex items-center gap-2">
                <span class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-sky-500/15 text-sky-300 border border-sky-500/30">TRAINING HUB</span>
                <h3 class="text-sm font-bold text-slate-100">📚 群聊风格实时训练中心 (特定群对话复刻)</h3>
              </div>
              <p class="text-xs text-slate-400 mt-1">自动采集指定 QQ 群全量聊天，构建微调对话对 (.jsonl) 并提炼群黑话与短接梗池。</p>
            </div>
            <label class="relative inline-flex items-center cursor-pointer">
              <input type="checkbox" id="training-master-toggle" class="sr-only peer" onchange="toggleTrainingMaster(this.checked)">
              <div class="w-11 h-6 bg-surface-700 rounded-full peer peer-checked:bg-emerald-500 after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:after:translate-x-5"></div>
            </label>
          </div>

          <div class="space-y-3">
            <div class="flex gap-2">
              <input id="input-train-gid" type="text" placeholder="输入要训练的群号（如 971590027）" class="flex-1 px-3 py-1.5 rounded-xl bg-surface-950 border border-surface-700 text-xs font-mono text-slate-100 focus:outline-none focus:border-sky-500" />
              <button onclick="triggerHistoryTrain()" id="btn-trigger-train" class="tactile-btn px-4 py-1.5 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs flex items-center gap-1.5">
                <span>⚡ 抓取120条并训练</span>
              </button>
            </div>

            <!-- 群训练监控列表 -->
            <div id="training-groups-table" class="space-y-2 max-h-[160px] overflow-y-auto pr-1"></div>
          </div>
        </div>

        <!-- 右侧 5 cols: 默认测试 QQ 实时对话快捷测试 -->
        <div class="lg:col-span-5 rounded-2xl border border-surface-700 bg-surface-900/90 p-5 flex flex-col justify-between space-y-3">
          <div>
            <div class="flex items-center justify-between">
              <h3 class="text-sm font-bold text-slate-100">💬 默认 QQ 实时对话输出测试</h3>
              <span id="superuser-qq-badge" class="px-2 py-0.5 rounded text-[11px] font-mono bg-sky-500/10 text-sky-300 border border-sky-500/30">QQ: 1785381042</span>
            </div>
            <p class="text-xs text-slate-400 mt-1">直接使用当前模式（大肥鱼闲聊 / 干员）向您的默认 QQ 发送测试消息，验证回复效果。</p>
            <div class="mt-3">
              <input id="input-test-chat" type="text" value="阿米娅，大肥鱼批发好玩吗？" placeholder="输入测试消息..." class="w-full px-3 py-2 rounded-xl bg-surface-950 border border-surface-700 text-xs text-slate-100 focus:outline-none focus:border-sky-500 font-mono" />
            </div>
          </div>
          <div class="flex items-center justify-between pt-2 border-t border-surface-800">
            <span id="test-chat-status" class="text-xs font-mono text-slate-400">点击发送即可在手机QQ查收</span>
            <button onclick="sendTestChatToDefaultQQ()" id="btn-send-test-chat" class="tactile-btn px-4 py-1.5 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 font-semibold text-xs">
              🚀 发送到 1785381042
            </button>
          </div>
        </div>
      </div>

      <!-- 底部两列：AI 最新对话流 + 后端实时事件终端 -->
      <div class="grid grid-cols-1 lg:grid-cols-12 gap-4">
        <div class="lg:col-span-6 rounded-2xl border border-surface-700 bg-surface-900/90 p-5">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-sm font-bold text-slate-100">AI 最新对话流</h3>
            <span class="font-mono text-xs text-slate-400" id="dialogue-count-label">0 条记录</span>
          </div>
          <div id="dialogue-list-box" class="space-y-2.5 max-h-[280px] overflow-y-auto pr-1"></div>
        </div>

        <div class="lg:col-span-6 rounded-2xl border border-surface-700 bg-surface-900/90 p-5">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-sm font-bold text-slate-100">后端实时事件与日志终端</h3>
            <span class="font-mono text-xs text-slate-400" id="log-count-label">0 条事件</span>
          </div>
          <div id="log-list-box" class="p-3 rounded-xl bg-surface-950 border border-surface-800 font-mono text-[11px] space-y-1.5 max-h-[280px] overflow-y-auto"></div>
        </div>
      </div>
    </section>

    <!-- =================================================================== -->
    <!-- 工作区 2：🎙️ 语音模式 (Voice & Acoustic Workspace)                  -->
    <!-- =================================================================== -->
    <section id="workspace-voice" class="hidden space-y-6">

      <!-- 顶部语音核心控制 Bento (3 Cells) -->
      <div class="grid grid-cols-1 lg:grid-cols-12 gap-4">
        <!-- Cell 1: QQ 语音回复主开关与发送策略 (5 cols) -->
        <div class="lg:col-span-5 rounded-2xl border border-surface-700 bg-surface-900/90 p-5 flex flex-col justify-between">
          <div>
            <div class="flex items-start justify-between">
              <div>
                <span class="font-mono text-[11px] uppercase tracking-wider text-emerald-400">VOICE SYNTHESIS ENGINE</span>
                <h2 class="text-xl font-bold tracking-tight mt-1">QQ 语音回复模式</h2>
                <p class="text-xs text-slate-400 mt-1">开启后，150 字以内的日常回复将自动通过 GPT-SoVITS 合成角色语音发出。</p>
              </div>
              <label class="relative inline-flex items-center cursor-pointer mt-1">
                <input type="checkbox" id="voice-main-toggle" class="sr-only peer" onchange="saveVoiceConfig()">
                <div class="w-12 h-6 bg-surface-700 rounded-full peer peer-checked:bg-emerald-500 after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:after:translate-x-6"></div>
              </label>
            </div>

            <div class="mt-4 pt-4 border-t border-surface-800 flex items-center justify-between">
              <div>
                <div class="text-xs font-semibold text-slate-200">纯语音沉浸回复模式 (Voice-Only)</div>
                <div class="text-[11px] text-slate-400">开启后短句只发语音条不发文字（若合成超时自动降级发文字）</div>
              </div>
              <input type="checkbox" id="voice-only-toggle" class="w-4 h-4 rounded accent-emerald-500" onchange="saveVoiceConfig()" />
            </div>
          </div>

          <div class="mt-4 pt-3 border-t border-surface-800 flex items-center justify-between text-xs font-mono text-slate-400">
            <span>短句同步阈值: &le;60字</span>
            <span>中长句首句截断: &le;45字</span>
          </div>
        </div>

        <!-- Cell 2: 9880 推理服务与 9876 语音工作室进程监控与启停 (4 cols) -->
        <div class="lg:col-span-4 rounded-2xl border border-surface-700 bg-surface-900/90 p-5 flex flex-col justify-between space-y-3">
          <div>
            <h2 class="text-base font-bold text-slate-100">SoVITS 本地声学服务节点</h2>
            <p class="text-xs text-slate-400 mt-0.5">9880 负责机器人实时发语音，9876 为多参考融合与批量调音台。</p>
          </div>

          <!-- 9880 状态行 -->
          <div class="p-3 rounded-xl bg-surface-950 border border-surface-800 flex items-center justify-between">
            <div>
              <div class="flex items-center gap-2">
                <span id="dot-9880" class="w-2 h-2 rounded-full bg-red-500"></span>
                <span class="text-xs font-bold font-mono">9880 GPT-SoVITS API</span>
              </div>
              <div id="text-9880" class="text-[11px] text-slate-400 mt-0.5">检测中...</div>
            </div>
            <div class="flex gap-1.5">
              <button onclick="controlVoiceService('9880', 'start')" class="tactile-btn px-2.5 py-1 rounded-lg bg-emerald-500/15 border border-emerald-500/30 text-emerald-300 text-xs font-medium">启动</button>
              <button onclick="controlVoiceService('9880', 'stop')" class="tactile-btn px-2.5 py-1 rounded-lg bg-surface-800 text-slate-300 text-xs">停止</button>
            </div>
          </div>

          <!-- 9876 状态行 -->
          <div class="p-3 rounded-xl bg-surface-950 border border-surface-800 flex items-center justify-between">
            <div>
              <div class="flex items-center gap-2">
                <span id="dot-9876" class="w-2 h-2 rounded-full bg-red-500"></span>
                <span class="text-xs font-bold font-mono">9876 Voice Studio Pro</span>
              </div>
              <div id="text-9876" class="text-[11px] text-slate-400 mt-0.5">检测中...</div>
            </div>
            <div class="flex gap-1.5">
              <button onclick="controlVoiceService('9876', 'start')" class="tactile-btn px-2.5 py-1 rounded-lg bg-emerald-500/15 border border-emerald-500/30 text-emerald-300 text-xs font-medium">启动</button>
              <a href="http://127.0.0.1:9876" target="_blank" class="tactile-btn px-2.5 py-1 rounded-lg bg-emerald-500 text-slate-950 text-xs font-semibold">打开 9876 ↗</a>
            </div>
          </div>
        </div>

        <!-- Cell 3: 情绪参考路由策略 (3 cols) -->
        <div class="lg:col-span-3 rounded-2xl border border-surface-700 bg-surface-900/90 p-5 flex flex-col justify-between">
          <div>
            <h2 class="text-base font-bold text-slate-100">情绪音色路由策略</h2>
            <p class="text-xs text-slate-400 mt-1">AI 根据完整回复选择情绪。参考音频仅在手动试听时锁定。</p>
            <label class="block text-xs text-slate-300 mt-3" for="voice-model-select">语音模型</label>
            <select id="voice-model-select" onchange="saveVoiceConfig()" class="w-full mt-2 px-3 py-2 rounded-xl bg-surface-950 border border-surface-700 text-xs text-slate-200"><option value="auto">跟随当前角色</option></select>
            <div class="mt-3 space-y-2">
              <label class="flex items-center gap-2.5 p-2.5 rounded-xl bg-surface-950 border border-surface-800 cursor-pointer">
                <input type="radio" name="voice_mode_radio" value="emotion_auto" checked onchange="saveVoiceConfig()" class="accent-emerald-500" />
                <div>
                  <div class="text-xs font-semibold text-slate-100">智能情绪自适应 (推荐)</div>
                  <div class="text-[11px] text-slate-400">根据整段语义选择参考，不按称呼匹配</div>
                </div>
              </label>
            </div>
          </div>
          <div class="mt-3">
            <select id="voice-ref-select" onchange="saveVoiceConfig()" class="w-full px-3 py-2 rounded-xl bg-surface-950 border border-surface-700 text-xs font-mono text-slate-200 focus:outline-none focus:border-emerald-500"></select>
          </div>
        </div>
      </div>

      <!-- 下部两列：声学采样精调滑块 + 实时在线试音台 -->
      <div class="grid grid-cols-1 lg:grid-cols-12 gap-4">
        <!-- 声学采样参数滑块 (5 cols) -->
        <div class="lg:col-span-5 rounded-2xl border border-surface-700 bg-surface-900/90 p-5 space-y-4">
          <div class="flex items-center justify-between">
            <div>
              <h3 class="text-sm font-bold text-slate-100">声学采样参数实时调控</h3>
              <p class="text-xs text-slate-400">调节后立即作用于机器人在 QQ 群内的下一条语音回复。</p>
            </div>
            <button onclick="resetVoiceParams()" class="tactile-btn px-2.5 py-1 rounded-lg border border-surface-700 bg-surface-850 text-[11px] font-mono text-slate-300">恢复默认</button>
          </div>

          <div class="space-y-3 pt-1">
            <div>
              <div class="flex justify-between text-xs font-mono mb-1">
                <span class="text-slate-300">语速倍率 (speed_factor)</span>
                <span id="val-voice-speed" class="text-emerald-400">1.00x</span>
              </div>
              <input id="slider-voice-speed" type="range" min="0.70" max="1.40" step="0.02" value="1.00" oninput="document.getElementById('val-voice-speed').innerText=parseFloat(this.value).toFixed(2)+'x'" onchange="saveVoiceConfig()" class="w-full" />
            </div>

            <div>
              <div class="flex justify-between text-xs font-mono mb-1">
                <span class="text-slate-300">情绪起伏温度 (temperature)</span>
                <span id="val-voice-temp" class="text-emerald-400">0.90</span>
              </div>
              <input id="slider-voice-temp" type="range" min="0.40" max="1.30" step="0.05" value="0.90" oninput="document.getElementById('val-voice-temp').innerText=parseFloat(this.value).toFixed(2)" onchange="saveVoiceConfig()" class="w-full" />
            </div>

            <div>
              <div class="flex justify-between text-xs font-mono mb-1">
                <span class="text-slate-300">采样收敛度 (top_p)</span>
                <span id="val-voice-topp" class="text-emerald-400">0.95</span>
              </div>
              <input id="slider-voice-topp" type="range" min="0.50" max="1.00" step="0.05" value="0.95" oninput="document.getElementById('val-voice-topp').innerText=parseFloat(this.value).toFixed(2)" onchange="saveVoiceConfig()" class="w-full" />
            </div>
          </div>
        </div>

        <!-- 在线语音合成试听台 (7 cols) -->
        <div class="lg:col-span-7 rounded-2xl border border-surface-700 bg-surface-900/90 p-5 flex flex-col justify-between space-y-4">
          <div>
            <div class="flex items-center justify-between">
              <div>
                <h3 class="text-sm font-bold text-slate-100">实时在线试音台 (Live Audition Deck)</h3>
                <p class="text-xs text-slate-400">直接调用 9880 接口与当前声学参数合成试听音频，检验不同情绪参考音频的真实咬字与气口表现。</p>
              </div>
              <span id="preview-status-tag" class="font-mono text-xs text-emerald-400">READY</span>
            </div>
            <textarea id="preview-tts-text" rows="3" class="w-full mt-3 p-3 rounded-xl bg-surface-950 border border-surface-700 text-xs text-slate-100 focus:outline-none focus:border-emerald-500">哼，今天算你表现不错！不过别得意太早，早点休息，明天还有新的委托等着我们呢。</textarea>
          </div>

          <div class="flex items-center justify-between gap-4 pt-1">
            <audio id="preview-audio-player" controls class="h-9 flex-1 rounded-lg"></audio>
            <button id="btn-tts-preview" onclick="runVoicePreview()" class="tactile-btn px-5 py-2.5 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-xs whitespace-nowrap">
              ▶ 立即合成试听
            </button>
          </div>
        </div>
      </div>
    </section>

  </main>

  <script>
    let currentWorkspace = 'bot';

    function showToast(msg) {
      const box = document.getElementById('toast-box');
      box.innerText = msg;
      box.classList.remove('hidden');
      clearTimeout(window._toastTimer);
      window._toastTimer = setTimeout(() => box.classList.add('hidden'), 2800);
    }

    function switchWorkspace(mode) {
      currentWorkspace = mode;
      document.body.setAttribute('data-workspace', mode);
      const botSec = document.getElementById('workspace-bot');
      const voiceSec = document.getElementById('workspace-voice');
      const btnBot = document.getElementById('tab-btn-bot');
      const btnVoice = document.getElementById('tab-btn-voice');

      if (mode === 'bot') {
        botSec.classList.remove('hidden');
        voiceSec.classList.add('hidden');
        btnBot.className = 'tactile-btn px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 bg-sky-500 text-slate-950 shadow-sm';
        btnVoice.className = 'tactile-btn px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 text-slate-400 hover:text-slate-200';
      } else {
        botSec.classList.add('hidden');
        voiceSec.classList.remove('hidden');
        btnVoice.className = 'tactile-btn px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 bg-emerald-500 text-slate-950 shadow-sm';
        btnBot.className = 'tactile-btn px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-2 text-slate-400 hover:text-slate-200';
      }
    }

    async function apiPost(path, payload) {
      const resp = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {})
      });
      return await resp.json();
    }

    async function refreshDashboard() {
      try {
        const resp = await fetch('/api/dashboard');
        const d = await resp.json();

        // 顶部状态
        const badge = document.getElementById('header-conn-badge');
        if (d.bot_connected) {
          badge.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-mono bg-emerald-500/10 text-emerald-400 border border-emerald-500/30';
          badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>全链路在线 (' + (d.connected_bots?.[0] || 'Bot') + ')';
        } else {
          badge.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-mono bg-amber-500/10 text-amber-400 border border-amber-500/30';
          badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>8080在线 · 等待NapCat接入';
        }

        // 机器人模式状态
        document.getElementById('bot-global-toggle').checked = !!d.global_active;
        document.getElementById('active-char-badge').innerText = d.active_character || 'none';
        document.querySelectorAll('.char-btn').forEach(btn => {
          const isCur = btn.getAttribute('data-char') === (d.active_character || 'none');
          btn.className = isCur
            ? 'char-btn tactile-btn px-3 py-2 rounded-xl border border-sky-500 bg-sky-500/15 text-left'
            : 'char-btn tactile-btn px-3 py-2 rounded-xl border border-surface-700 bg-surface-850 hover:border-surface-600 text-left';
        });

        // 已归档立绘 (仅保留数量统计，不再平铺冗余角色名)
        const refs = d.saved_refs || [];
        document.getElementById('info-refs-count').innerText = refs.length + ' 张';

        // 群白名单
        const groups = d.active_groups || [];
        document.getElementById('group-count-badge').innerText = groups.length + ' 个白名单群';
        document.getElementById('active-groups-list').innerHTML = groups.length
          ? groups.map(g => `<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-surface-850 border border-surface-700 font-mono text-xs text-slate-200">${g}<button onclick="modifyGroup('del','${g}')" class="text-slate-400 hover:text-red-400 ml-1">×</button></span>`).join('')
          : '<span class="text-xs text-slate-500">暂无白名单群</span>';

        // 安慰模式目标
        const comforts = d.comfort_targets || [];
        document.getElementById('comfort-targets-list').innerHTML = comforts.length
          ? comforts.map(c => `<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-sky-500/10 border border-sky-500/30 font-mono text-xs text-sky-300">💙 ${c}<button onclick="modifyComfort(false,'${c}')" class="text-sky-400 hover:text-red-400 ml-1">×</button></span>`).join('')
          : '<span class="text-xs text-slate-500">当前未绑定定向安慰对象</span>';

        // 已提取画像的群
        const gPersonas = d.group_personas || [];
        document.getElementById('extracted-groups-pills').innerText = gPersonas.length ? `已有画像: ${gPersonas.join(', ')}` : '';

        // AI 最新对话
        const dialogues = (d.recent_dialogues || []).slice().reverse();
        document.getElementById('dialogue-count-label').innerText = dialogues.length + ' 条记录';
        document.getElementById('dialogue-list-box').innerHTML = dialogues.length
          ? dialogues.map(item => `
            <div class="p-3 rounded-xl bg-surface-950 border border-surface-800 space-y-1">
              <div class="flex items-center justify-between text-[11px] font-mono text-slate-400">
                <span>[${item.source}] 用户 ${item.sender_id}</span>
                <span>${item.time} ${item.voice ? '🎙️' : ''}</span>
              </div>
              <div class="text-xs text-slate-300">💬 ${item.user_text}</div>
              <div class="text-xs text-sky-300 pt-0.5">🤖 [${item.character}] ${item.reply}</div>
            </div>
          `).join('')
          : '<div class="text-xs text-slate-500 py-8 text-center">暂无对话记录，在白名单群内 @机器人 即可触发实时展示</div>';

        // 实时日志
        const logs = (d.recent_logs || []).slice().reverse();
        document.getElementById('log-count-label').innerText = logs.length + ' 条事件';
        document.getElementById('log-list-box').innerHTML = logs.length
          ? logs.map(l => {
              const col = l.level === 'ERROR' ? 'text-red-400' : (l.level === 'WARN' ? 'text-amber-400' : 'text-slate-300');
              return `<div class="${col}">[${l.time}] ${l.text}</div>`;
            }).join('')
          : '<div class="text-slate-500">等待事件进入...</div>';

        // 语音模式状态同步
        document.getElementById('voice-main-toggle').checked = !!d.voice_enabled;
        document.getElementById('voice-only-toggle').checked = !!d.voice_only_reply;
        document.getElementById('voice-tab-dot').className = d.voice_enabled
          ? 'w-2 h-2 rounded-full bg-emerald-400'
          : 'w-2 h-2 rounded-full bg-slate-600';

        // 9880 / 9876 在线状态
        const ok9880 = !!d.tts_9880_online;
        const ok9876 = !!d.studio_9876_online;
        document.getElementById('dot-9880').className = ok9880 ? 'w-2 h-2 rounded-full bg-emerald-400' : 'w-2 h-2 rounded-full bg-red-500';
        document.getElementById('text-9880').innerText = ok9880 ? '运行正常 · 实时合成就绪' : '未运行 · 点击右侧一键拉起';
        document.getElementById('dot-9876').className = ok9876 ? 'w-2 h-2 rounded-full bg-emerald-400' : 'w-2 h-2 rounded-full bg-slate-500';
        document.getElementById('text-9876').innerText = ok9876 ? '运行正常 · 支持多参考融合调音' : '未启动（可选专业调音台）';

        // 语音参考下拉框与参数
        const modelSelect = document.getElementById('voice-model-select');
        const modelOptions = [{id: 'auto', name: '跟随当前角色'}, ...(d.voice_models || []).filter(m => m.ready)];
        const modelKey = JSON.stringify(modelOptions);
        if (modelSelect.dataset.options !== modelKey) {
          modelSelect.replaceChildren(...modelOptions.map(m => new Option(m.name, m.id)));
          modelSelect.dataset.options = modelKey;
        }
        if (document.activeElement !== modelSelect) modelSelect.value = d.voice_model || 'auto';
        const vRefs = d.voice_refs || [];
        const sel = document.getElementById('voice-ref-select');
        const refKey = JSON.stringify(vRefs);
        if (sel.dataset.options !== refKey) {
          sel.replaceChildren(new Option('AI 根据台词自动选择（推荐）', ''), ...vRefs.map(r => new Option(`${r.label} — ${r.text.slice(0, 22)}...`, r.id)));
          sel.dataset.options = refKey;
        }

        const radios = document.getElementsByName('voice_mode_radio');
        radios.forEach(r => { r.checked = (r.value === (d.voice_mode || 'emotion_auto')); });

        if (document.activeElement.tagName !== 'INPUT') {
          if (d.voice_speed) {
            document.getElementById('slider-voice-speed').value = d.voice_speed;
            document.getElementById('val-voice-speed').innerText = parseFloat(d.voice_speed).toFixed(2) + 'x';
          }
          if (d.voice_temperature) {
            document.getElementById('slider-voice-temp').value = d.voice_temperature;
            document.getElementById('val-voice-temp').innerText = parseFloat(d.voice_temperature).toFixed(2);
          }
          if (d.voice_top_p) {
            document.getElementById('slider-voice-topp').value = d.voice_top_p;
            document.getElementById('val-voice-topp').innerText = parseFloat(d.voice_top_p).toFixed(2);
          }
        }

        // 闲聊模式与风格状态同步
        const stToggle = document.getElementById('small-talk-toggle');
        if (stToggle) {
          stToggle.checked = !!d.small_talk_mode;
          const currentStyle = d.small_talk_style || 'pure_persona';
          const badge = document.getElementById('small-talk-status-badge');
          if (badge) {
            if (d.small_talk_mode) {
              badge.innerText = currentStyle === 'pure_persona' ? 'ON · 纯角色闲聊(5-20字)' : 'ON · 极短群友接梗';
              badge.className = 'px-2 py-0.5 rounded text-[10px] font-mono bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
            } else {
              badge.innerText = 'OFF · 原版干员模式';
              badge.className = 'px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-400';
            }
          }
          const btnPure = document.getElementById('btn-style-pure');
          const btnGroup = document.getElementById('btn-style-group');
          const styleDesc = document.getElementById('small-talk-style-desc');
          if (btnPure && btnGroup) {
            if (currentStyle === 'pure_persona') {
              btnPure.className = 'px-2.5 py-1 rounded text-[11px] font-medium transition-all bg-sky-500/20 text-sky-300 border border-sky-500/40';
              btnGroup.className = 'px-2.5 py-1 rounded text-[11px] font-medium transition-all text-slate-400 hover:text-slate-200';
              if (styleDesc) styleDesc.innerText = '🌸 纯角色设定 (5~20字，专属口吻)';
            } else {
              btnGroup.className = 'px-2.5 py-1 rounded text-[11px] font-medium transition-all bg-amber-500/20 text-amber-300 border border-amber-500/40';
              btnPure.className = 'px-2.5 py-1 rounded text-[11px] font-medium transition-all text-slate-400 hover:text-slate-200';
              if (styleDesc) styleDesc.innerText = '🐟 群友接梗模式 (大肥鱼风格)';
            }
          }
        }

        // 默认回复模式（免触发词）状态同步
        const arToggle = document.getElementById('auto-reply-toggle');
        if (arToggle) {
          arToggle.checked = d.auto_reply_mode !== false;
          const arBadge = document.getElementById('auto-reply-status-badge');
          if (arBadge) {
            arBadge.innerText = d.auto_reply_mode !== false ? 'ON · 免触发词 (3s)' : 'OFF · 需@唤醒';
            arBadge.className = d.auto_reply_mode !== false ? 'px-2 py-0.5 rounded text-[10px] font-mono bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : 'px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-400';
          }
        }

        // 群训练状态同步
        const trToggle = document.getElementById('training-master-toggle');
        if (trToggle) trToggle.checked = !!d.training_mode;
        renderTrainingHub(d.training_status || {}, d.training_groups || d.active_groups || []);

        // 默认测试 QQ 标识
        const sqqBadge = document.getElementById('superuser-qq-badge');
        if (sqqBadge && d.superuser_qq) {
          sqqBadge.innerText = '默认QQ: ' + d.superuser_qq;
        }

        // 群成员白名单总开关与选项同步
        const wlToggle = document.getElementById('group-user-wl-master-toggle');
        if (wlToggle) wlToggle.checked = !!d.group_user_whitelist_enabled;
        const wlStatusLabel = document.getElementById('group-user-wl-status-label');
        if (wlStatusLabel) {
          wlStatusLabel.innerText = d.group_user_whitelist_enabled ? '白名单过滤: 已启用' : '白名单过滤: 禁用';
          wlStatusLabel.className = d.group_user_whitelist_enabled
            ? 'text-xs font-mono text-emerald-400 font-semibold'
            : 'text-xs font-mono text-slate-400';
        }

        // 同步群白名单下拉列表选项
        const wlGroupSelect = document.getElementById('wl-group-select');
        if (wlGroupSelect) {
          const currentVal = wlGroupSelect.value;
          const groups = d.active_groups || [];
          const opts = ['<option value="">-- 选择白名单群 --</option>'].concat(
            groups.map(g => `<option value="${g}">群 ${g}</option>`)
          );
          const newHtml = opts.join('');
          if (wlGroupSelect.innerHTML !== newHtml) {
            wlGroupSelect.innerHTML = newHtml;
            if (currentVal && groups.includes(currentVal)) {
              wlGroupSelect.value = currentVal;
            } else if (groups.length > 0 && !currentWlGroupId) {
              wlGroupSelect.value = groups[0];
              currentWlGroupId = groups[0];
              const customInp = document.getElementById('wl-custom-group-input');
              if (customInp && !customInp.value) customInp.value = groups[0];
            }
          }
        }
      } catch (e) {
        console.error(e);
      }
    }

    let currentWlGroupId = '';
    let currentGroupMembers = [];
    let selectedWlUserIds = new Set();

    async function toggleGroupUserWhitelist(checked) {
      try {
        const res = await apiPost('/api/switch/group_user_whitelist', { enable: checked });
        showToast(res.enabled ? '🛡️ 群成员发言白名单过滤已开启' : '已关闭群成员白名单过滤');
        refreshDashboard();
      } catch (e) {
        showToast('切换白名单开关失败: ' + e);
      }
    }

    function onWhitelistGroupChange() {
      const sel = document.getElementById('wl-group-select');
      const val = (sel ? sel.value : '').trim();
      if (val) {
        currentWlGroupId = val;
        const customInp = document.getElementById('wl-custom-group-input');
        if (customInp) customInp.value = val;
        fetchGroupMembers();
      }
    }

    async function fetchGroupMembers() {
      const customGid = (document.getElementById('wl-custom-group-input').value || '').trim();
      const selGid = (document.getElementById('wl-group-select').value || '').trim();
      const gid = customGid || selGid || currentWlGroupId;
      if (!gid) {
        showToast('请先选择或输入群号');
        return;
      }
      currentWlGroupId = gid;
      const btn = document.getElementById('btn-fetch-members');
      const grid = document.getElementById('wl-members-grid');
      const stats = document.getElementById('wl-member-stats');
      if (btn) btn.disabled = true;
      if (stats) stats.innerText = `⏳ 正在读取群 ${gid} 成员列表...`;
      if (grid) grid.innerHTML = '<div class="col-span-full text-center py-10 text-xs text-sky-400">正在通过 OneBot API 检索群成员列表...</div>';

      try {
        const [memResp, wlResp] = await Promise.all([
          fetch(`/api/groups/members?group_id=${gid}`).then(r => r.json()),
          fetch(`/api/whitelist/users?group_id=${gid}`).then(r => r.json())
        ]);

        if (memResp.code !== 0) {
          if (grid) grid.innerHTML = `<div class="col-span-full text-center py-10 text-xs text-red-400">读取失败: ${memResp.message || '未知错误'}</div>`;
          if (stats) stats.innerText = `读取失败: ${memResp.message}`;
          return;
        }

        currentGroupMembers = memResp.members || [];
        const savedUsers = (wlResp.code === 0 && Array.isArray(wlResp.users)) ? wlResp.users : [];
        selectedWlUserIds = new Set(savedUsers.map(String));

        renderGroupMembers();
        showToast(`✅ 成功获取群 ${gid} 的 ${currentGroupMembers.length} 名成员`);
      } catch (e) {
        if (grid) grid.innerHTML = `<div class="col-span-full text-center py-10 text-xs text-red-400">网络异常: ${e}</div>`;
        if (stats) stats.innerText = `异常: ${e}`;
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    function renderGroupMembers() {
      const grid = document.getElementById('wl-members-grid');
      const stats = document.getElementById('wl-member-stats');
      if (!grid) return;

      const filterKw = (document.getElementById('wl-member-search')?.value || '').trim().toLowerCase();
      const visibleMembers = currentGroupMembers.filter(m => {
        if (!filterKw) return true;
        const name = (m.display_name || '').toLowerCase();
        const nick = (m.nickname || '').toLowerCase();
        const card = (m.card || '').toLowerCase();
        const uid = String(m.user_id);
        return name.includes(filterKw) || nick.includes(filterKw) || card.includes(filterKw) || uid.includes(filterKw);
      });

      if (stats) {
        stats.innerText = `群 ${currentWlGroupId} · 共 ${currentGroupMembers.length} 人 · 当前已勾选 ${selectedWlUserIds.size} 人 (当前显示 ${visibleMembers.length} 人)`;
      }

      if (!visibleMembers.length) {
        grid.innerHTML = '<div class="col-span-full text-center py-8 text-xs text-slate-500">未找到匹配的群成员</div>';
        return;
      }

      grid.innerHTML = visibleMembers.map(m => {
        const uid = String(m.user_id);
        const isSelected = selectedWlUserIds.has(uid);
        const roleBadge = m.role === 'owner'
          ? '<span class="text-[9px] px-1 py-0.2 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">群主</span>'
          : (m.role === 'admin'
            ? '<span class="text-[9px] px-1 py-0.2 rounded bg-sky-500/20 text-sky-300 border border-sky-500/30">管理</span>'
            : '');
        
        const cardClass = isSelected
          ? 'cursor-pointer p-2 rounded-xl border border-sky-500 bg-sky-500/15 hover:bg-sky-500/20 transition-all flex items-center gap-2 relative shadow'
          : 'cursor-pointer p-2 rounded-xl border border-surface-800 bg-surface-900/80 hover:border-surface-700 hover:bg-surface-850 transition-all flex items-center gap-2 relative';

        const checkMark = isSelected
          ? '<div class="w-4 h-4 rounded-md bg-sky-500 text-slate-950 flex items-center justify-center font-bold text-[10px] shrink-0">✓</div>'
          : '<div class="w-4 h-4 rounded-md border border-surface-700 bg-surface-950 shrink-0"></div>';

        return `
          <div onclick="toggleMemberSelection('${uid}')" class="${cardClass}">
            <img src="https://q1.qlogo.cn/g?b=qq&nk=${uid}&s=100" class="w-7 h-7 rounded-full bg-surface-800 shrink-0" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\\'http://www.w3.org/2000/svg\\' viewBox=\\'0 0 24 24\\' fill=\\'%2364748b\\'><circle cx=\\'12\\' cy=\\'8\\' r=\\'4\\'/><path d=\\'M4 20c0-4 4-6 8-6s8 2 8 6\\'/></svg>'" />
            <div class="flex-1 min-w-0 pr-1">
              <div class="flex items-center gap-1">
                <span class="text-xs font-semibold text-slate-200 truncate" title="${m.display_name}">${m.display_name}</span>
                ${roleBadge}
              </div>
              <div class="text-[10px] font-mono text-slate-400 truncate">${uid}</div>
            </div>
            ${checkMark}
          </div>
        `;
      }).join('');
    }

    function toggleMemberSelection(userId) {
      const uid = String(userId);
      if (selectedWlUserIds.has(uid)) {
        selectedWlUserIds.delete(uid);
      } else {
        selectedWlUserIds.add(uid);
      }
      renderGroupMembers();
    }

    function selectAllMembers(selectAll) {
      const filterKw = (document.getElementById('wl-member-search')?.value || '').trim().toLowerCase();
      const targetList = currentGroupMembers.filter(m => {
        if (!filterKw) return true;
        const name = (m.display_name || '').toLowerCase();
        const nick = (m.nickname || '').toLowerCase();
        const card = (m.card || '').toLowerCase();
        const uid = String(m.user_id);
        return name.includes(filterKw) || nick.includes(filterKw) || card.includes(filterKw) || uid.includes(filterKw);
      });

      targetList.forEach(m => {
        const uid = String(m.user_id);
        if (selectAll) {
          selectedWlUserIds.add(uid);
        } else {
          selectedWlUserIds.delete(uid);
        }
      });
      renderGroupMembers();
    }

    function invertMemberSelection() {
      const filterKw = (document.getElementById('wl-member-search')?.value || '').trim().toLowerCase();
      const targetList = currentGroupMembers.filter(m => {
        if (!filterKw) return true;
        const name = (m.display_name || '').toLowerCase();
        const nick = (m.nickname || '').toLowerCase();
        const card = (m.card || '').toLowerCase();
        const uid = String(m.user_id);
        return name.includes(filterKw) || nick.includes(filterKw) || card.includes(filterKw) || uid.includes(filterKw);
      });

      targetList.forEach(m => {
        const uid = String(m.user_id);
        if (selectedWlUserIds.has(uid)) {
          selectedWlUserIds.delete(uid);
        } else {
          selectedWlUserIds.add(uid);
        }
      });
      renderGroupMembers();
    }

    function filterGroupMembers() {
      renderGroupMembers();
    }

    async function saveGroupUserWhitelist() {
      const gid = currentWlGroupId || (document.getElementById('wl-custom-group-input').value || '').trim();
      if (!gid) {
        showToast('请先选择或输入群号');
        return;
      }
      const users = Array.from(selectedWlUserIds);
      const btn = document.getElementById('btn-save-user-wl');
      const statusEl = document.getElementById('wl-save-status-msg');
      if (btn) btn.disabled = true;
      if (statusEl) statusEl.innerText = '正在保存白名单...';

      try {
        const res = await apiPost('/api/whitelist/users', { group_id: gid, users: users });
        if (res.code === 0) {
          showToast(`💾 群 ${gid} 白名单已保存！(共 ${users.length} 人)`);
          if (statusEl) statusEl.innerText = `✅ 已于 ${new Date().toLocaleTimeString()} 保存 ${users.length} 个白名单QQ`;
          setTimeout(() => { if (statusEl) statusEl.innerText = ''; }, 4000);
          refreshDashboard();
        } else {
          showToast('保存失败: ' + (res.message || '未知错误'));
          if (statusEl) statusEl.innerText = '保存失败: ' + (res.message || '未知错误');
        }
      } catch (e) {
        showToast('保存异常: ' + e);
        if (statusEl) statusEl.innerText = '请求异常: ' + e;
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function toggleSmallTalk(enabled) {
      try {
        const res = await apiPost('/api/switch/small_talk', { enable: enabled });
        showToast(res.small_talk_mode ? '💬 日常闲聊模式已开启 (DeepSeek 极速响应)' : '🤖 已恢复原版干员模式');
        refreshDashboard();
      } catch (e) {
        showToast('切换失败: ' + e);
      }
    }

    async function setSmallTalkStyle(style) {
      try {
        const res = await apiPost('/api/switch/small_talk_style', { style: style });
        showToast(res.small_talk_style === 'pure_persona' ? '🌸 已切换为纯角色设定闲聊 (5~20字，无群黑话)' : '🐟 已切换为群友接梗模式 (大肥鱼风格)');
        refreshDashboard();
      } catch (e) {
        showToast('切换闲聊风格失败: ' + e);
      }
    }

    async function toggleAutoReply(enabled) {
      try {
        const res = await apiPost('/api/switch/auto_reply', { enable: enabled });
        showToast(res.auto_reply_mode ? '⚡ 默认回复模式已开启 (免触发词 / 3s冷却防刷屏)' : '🔒 已切换为需@唤醒模式');
        refreshDashboard();
      } catch (e) {
        showToast('切换默认回复模式失败: ' + e);
      }
    }

    async function toggleTrainingMaster(enabled) {
      try {
        const res = await apiPost('/api/training/toggle', { enable: enabled });
        showToast(res.training_mode ? '📚 群聊风格实时采集与训练已开启' : '已关闭群聊训练模式');
        refreshDashboard();
      } catch (e) {
        showToast('切换训练模式失败: ' + e);
      }
    }

    async function triggerHistoryTrain(targetGid) {
      const gid = targetGid || (document.getElementById('input-train-gid').value || '').trim();
      if (!gid) {
        alert('请输入或选择要训练的群号');
        return;
      }
      const btn = document.getElementById('btn-trigger-train');
      if (btn) btn.disabled = true;
      showToast('⏳ 正在抓取群 ' + gid + ' 历史消息并提炼群黑话画像...');
      try {
        const res = await apiPost('/api/training/trigger', { group_id: gid, count: 120 });
        if (res.code === 0) {
          showToast('✅ 群 ' + gid + ' 风格训练完成！已更新黑话画像与短句池');
        } else {
          showToast('训练返回: ' + (res.message || '完成'));
        }
        refreshDashboard();
      } catch (e) {
        showToast('训练请求异常: ' + e);
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    function renderTrainingHub(tStatus, trainingGroups) {
      const box = document.getElementById('training-groups-table');
      if (!box) return;
      const groups = tStatus.groups || [];
      if (!groups.length) {
        box.innerHTML = '<div class="text-xs text-slate-500 py-3 text-center">暂无已采集训练记录，输入群号点击上方按钮即可一键抓取并训练</div>';
        return;
      }
      box.innerHTML = groups.map(g => {
        const isTarget = (trainingGroups || []).includes(String(g.group_id));
        const profTag = g.has_trained_profile
          ? '<span class="text-emerald-400 font-bold">已生成群画像</span>'
          : '<span class="text-slate-500">未训练</span>';
        return `
          <div class="p-2.5 rounded-xl bg-surface-950 border border-surface-800 flex items-center justify-between text-xs">
            <div>
              <div class="flex items-center gap-2">
                <span class="font-mono font-bold text-slate-200">群 ${g.group_id}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded bg-surface-800 text-slate-400">${profTag}</span>
              </div>
              <div class="text-[11px] font-mono text-slate-400 mt-1">
                语料: ${g.raw_lines} 行 · SFT对话对: ${g.dataset_pairs} 条
                ${g.last_trained_at ? '· 上次训练: ' + g.last_trained_at.slice(5, 16) : ''}
              </div>
            </div>
            <div class="flex items-center gap-1.5">
              <button onclick="triggerHistoryTrain('${g.group_id}')" class="tactile-btn px-2.5 py-1 rounded-lg bg-sky-500/15 hover:bg-sky-500/25 border border-sky-500/30 text-sky-300 text-[11px]">
                ⚡ 立即训练
              </button>
            </div>
          </div>
        `;
      }).join('');
    }

    async function sendTestChatToDefaultQQ() {
      const text = (document.getElementById('input-test-chat').value || '').trim();
      if (!text) return;
      const stMsg = document.getElementById('test-chat-status');
      const btn = document.getElementById('btn-send-test-chat');
      if (btn) btn.disabled = true;
      if (stMsg) stMsg.innerText = '正在生成并投递私聊...';
      try {
        const resp = await fetch('/api/test/send_private_chat?text=' + encodeURIComponent(text));
        const res = await resp.json();
        if (res.code === 0) {
          if (stMsg) stMsg.innerText = '已发送！Bot 回复: ' + (res.reply_text || '无');
          showToast('✅ 对话已发送至 QQ ' + res.uid + '！');
        } else {
          if (stMsg) stMsg.innerText = '发送失败: ' + (res.error || res.message);
        }
        refreshDashboard();
      } catch (e) {
        if (stMsg) stMsg.innerText = '请求异常: ' + e;
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function toggleGlobal(active) {
      await apiPost('/api/switch/global', { active });
      showToast(active ? '已开启机器人全局响应' : '已切换为全局静默模式');
      refreshDashboard();
    }

    async function setCharacter(character) {
      await apiPost('/api/switch/character', { character });
      showToast('已切换当前人格模式: ' + character);
      refreshDashboard();
    }

    async function modifyGroup(action, gid) {
      const groupId = gid || document.getElementById('input-group-id').value.trim();
      if (!groupId) return showToast('请输入有效的 QQ 群号');
      await apiPost('/api/switch/group', { action, group_id: groupId });
      document.getElementById('input-group-id').value = '';
      showToast(action === 'add' ? `已将群 ${groupId} 加入白名单` : `已移除群 ${groupId}`);
      refreshDashboard();
    }

    async function modifyComfort(enable, qq) {
      const target = qq || document.getElementById('input-comfort-qq').value.trim();
      if (!target) return showToast('请输入 QQ 号');
      await apiPost('/api/switch/comfort', { target, enable });
      document.getElementById('input-comfort-qq').value = '';
      showToast(enable ? `已为 ${target} 开启安慰模式` : `已解除 ${target} 安慰绑定`);
      refreshDashboard();
    }

    function loadTxtFile(input) {
      const file = input.files?.[0];
      if (!file) return;
      const groupGuess = file.name.replace(/\.[^.]+$/, '');
      if (/^\d{5,12}$/.test(groupGuess) && !document.getElementById('extract-group-id').value) {
        document.getElementById('extract-group-id').value = groupGuess;
      }
      const reader = new FileReader();
      reader.onload = e => {
        document.getElementById('extract-raw-text').value = e.target.result || '';
        showToast('已加载聊天记录文件: ' + file.name);
      };
      reader.readAsText(file, 'utf-8');
    }

    async function submitGroupExtract() {
      const gid = document.getElementById('extract-group-id').value.trim();
      const raw = document.getElementById('extract-raw-text').value.trim();
      if (!gid || !raw) return showToast('请填写群号并粘贴或选择聊天记录文本');
      const statusEl = document.getElementById('extract-status-msg');
      statusEl.innerText = '正在清洗 CQ 码并提炼群画像...';
      const res = await apiPost('/api/extract/group', { group_id: gid, raw_text: raw });
      if (res.code === 0) {
        statusEl.innerText = `✅ 已生成 ${gid}.json (${res.result?.lines || 0} 行有效记录)`;
        showToast(`群 ${gid} 画像提炼完成`);
        refreshDashboard();
      } else {
        statusEl.innerText = '❌ ' + (res.message || '提炼失败');
      }
    }

    async function saveVoiceConfig() {
      const enabled = document.getElementById('voice-main-toggle').checked;
      const voiceOnly = document.getElementById('voice-only-toggle').checked;
      let vMode = 'emotion_auto';
      document.getElementsByName('voice_mode_radio').forEach(r => { if (r.checked) vMode = r.value; });
      const refName = document.getElementById('voice-ref-select').value || 'default';
      const speed = parseFloat(document.getElementById('slider-voice-speed').value || '1.0');
      const temp = parseFloat(document.getElementById('slider-voice-temp').value || '0.9');
      const topp = parseFloat(document.getElementById('slider-voice-topp').value || '0.95');

      await apiPost('/api/voice/config', {
        voice_model: document.getElementById('voice-model-select').value,
        voice_enabled: enabled,
        voice_only_reply: voiceOnly,
        voice_mode: vMode,
        voice_ref_name: refName,
        voice_speed: speed,
        voice_temperature: temp,
        voice_top_p: topp
      });
      showToast('已同步语音模式声学配置');
      refreshDashboard();
    }

    async function resetVoiceParams() {
      document.getElementById('slider-voice-speed').value = 1.00;
      document.getElementById('val-voice-speed').innerText = '1.00x';
      document.getElementById('slider-voice-temp').value = 0.90;
      document.getElementById('val-voice-temp').innerText = '0.90';
      document.getElementById('slider-voice-topp').value = 0.95;
      document.getElementById('val-voice-topp').innerText = '0.95';
      await saveVoiceConfig();
    }

    async function controlVoiceService(target, action) {
      showToast(`正在 ${action === 'start' ? '启动' : '停止'} ${target} 服务...`);
      const res = await apiPost('/api/voice/service', { target, action });
      showToast(res.message || '操作已发送');
      setTimeout(refreshDashboard, 1200);
    }

    async function runVoicePreview() {
      const text = document.getElementById('preview-tts-text').value.trim();
      if (!text) return showToast('请输入试听台词');
      const tag = document.getElementById('preview-status-tag');
      const btn = document.getElementById('btn-tts-preview');
      tag.innerText = 'SYNTHESIZING...';
      btn.disabled = true;
      try {
        const refName = document.getElementById('voice-ref-select').value || '';
        const res = await apiPost('/api/voice/preview', { text, ref_name: refName });
        if (res.code === 0 && res.audio_data_uri) {
          const player = document.getElementById('preview-audio-player');
          player.src = res.audio_data_uri;
          player.play();
          tag.innerText = 'DONE';
          showToast('语音合成成功，正在播放');
        } else {
          tag.innerText = 'OFFLINE / ERR';
          showToast(res.message || '合成失败，请确认 9880 服务已启动');
        }
      } finally {
        btn.disabled = false;
      }
    }

    async function restartBackend() {
      showToast('正在热重启 8080 后端...');
      await apiPost('/api/restart', {});
      setTimeout(refreshDashboard, 1800);
    }

    refreshDashboard();
    setInterval(refreshDashboard, 2000);
  </script>

  <!-- === 全局可拖拽文本框样式与 1.2s /api/progress 实时三链路轮询引擎 === -->
  <style>
    textarea,
    pre,
    .resizable-box,
    .log-box,
    .log-container,
    #logs,
    #log-list,
    #dialogue-list,
    .dialogue-item,
    .msg-bubble,
    .prompt-preview {
      resize: both !important;
      overflow: auto !important;
      max-height: none !important;
      text-overflow: unset !important;
      white-space: pre-wrap !important;
      word-break: break-word !important;
    }
    .rt-card {
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid rgba(51, 65, 85, 0.9);
      border-radius: 14px;
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      resize: vertical;
      overflow: auto;
    }
    .rt-badge {
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .rt-badge.running { background: rgba(245, 158, 11, 0.2); color: #fcd34d; border: 1px solid rgba(245, 158, 11, 0.5); }
    .rt-badge.done { background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.5); }
    .rt-badge.idle { background: rgba(71, 85, 105, 0.25); color: #94a3b8; border: 1px solid rgba(100, 116, 139, 0.4); }
    .rt-badge.error { background: rgba(239, 68, 68, 0.2); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.5); }
    .rt-bar-bg {
      height: 7px;
      background: rgba(30, 41, 59, 0.95);
      border-radius: 999px;
      overflow: hidden;
      margin: 2px 0 4px;
    }
    .rt-bar-fill {
      height: 100%;
      background: linear-gradient(90deg, #38bdf8, #818cf8, #34d399);
      transition: width 0.3s ease;
    }
    .rt-field-label {
      font-size: 11px;
      color: #94a3b8;
      margin-top: 4px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .rt-textarea {
      width: 100%;
      min-height: 70px;
      background: rgba(2, 6, 23, 0.82);
      color: #f1f5f9;
      border: 1px solid rgba(71, 85, 105, 0.65);
      border-radius: 8px;
      padding: 7px 9px;
      font-size: 12px;
      line-height: 1.45;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      resize: both !important;
      overflow: auto !important;
      box-sizing: border-box;
    }
    .rt-textarea:focus {
      outline: none;
      border-color: #38bdf8;
    }
  </style>

  <script>
    (function() {
      let expandedAll = false;
      const savedHeights = {};

      const PIPELINE_DEFAULTS = [
        {
          slot: 'text',
          title: '📝 文字生成流水线',
          stage: '待机中 · 等待群聊/私聊消息触发（解析别称 → 百炼/DeepSeek推理 → 文档/消息下发）',
          percent: 0,
          status: 'idle',
          input_text: '',
          prompt_text: '',
          output_text: '',
          extra_info: '支持超过 650 字自动打包 .docx / .pdf 文档',
          steps: []
        },
        {
          slot: 'image',
          title: '🎨 二次元生图（4 阶段）',
          stage: '待机中 · 1/4 别称与refs立绘匹配 → 2/4 百炼Agent发散 → 3/4 深度思考渲染 → 4/4 压缩下发',
          percent: 0,
          status: 'idle',
          input_text: '',
          prompt_text: '',
          output_text: '',
          extra_info: 'Agent: 97fcb1dc96504f879c58f3b4c3a3e6d0 | 模型: qwen-image-2.1-pro',
          steps: []
        },
        {
          slot: 'voice',
          title: '🎙️ 语音合成（3 阶段）',
          stage: '待机中 · 1/3 情绪参考音频匹配 → 2/3 M5 MPS (:9880) 声学推理 → 3/3 SILK_v3 编码回传',
          percent: 0,
          status: 'idle',
          input_text: '',
          prompt_text: '',
          output_text: '',
          extra_info: '引擎: GPT-SoVITS v2ProPlus + pilk SILK_v3',
          steps: []
        }
      ];

      function fitTextarea(el) {
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = Math.max(74, Math.min(520, el.scrollHeight + 8)) + 'px';
      }

      window.toggleAllBoxesExpand = function() {
        expandedAll = !expandedAll;
        document.querySelectorAll('textarea, pre, .rt-textarea').forEach(el => {
          const k = el.getAttribute('data-box-key');
          if (expandedAll) {
            fitTextarea(el);
            if (k) savedHeights[k] = el.style.height;
          } else {
            el.style.height = '74px';
            if (k) delete savedHeights[k];
          }
        });
        if (typeof showToast === 'function') {
          showToast(expandedAll ? '已一键撑开全部文本框至完整高度' : '已恢复所有文本框默认高度');
        }
      };

      window.__toggleAllBoxesExpand = window.toggleAllBoxesExpand;

      function bindResizeObserver(container) {
        container.querySelectorAll('textarea[data-box-key]').forEach(ta => {
          const k = ta.getAttribute('data-box-key');
          if (savedHeights[k]) {
            ta.style.height = savedHeights[k];
          } else if (expandedAll) {
            fitTextarea(ta);
          }
          ta.addEventListener('mouseup', () => {
            if (ta.style.height) savedHeights[k] = ta.style.height;
          });
        });
      }

      function pickLatestByKind(tasks, kind) {
        for (const t of tasks) {
          if (t.kind === kind) return t;
        }
        return null;
      }

      function renderCards(tasks) {
        const container = document.getElementById('rt-cards-container');
        if (!container) return;

        // 记忆用户手动拖拽后的高度
        container.querySelectorAll('textarea[data-box-key]').forEach(ta => {
          const k = ta.getAttribute('data-box-key');
          if (k && ta.style.height) savedHeights[k] = ta.style.height;
        });

        const cardsData = PIPELINE_DEFAULTS.map(def => {
          const matched = pickLatestByKind(tasks, def.slot);
          return matched ? Object.assign({}, def, matched, { slot: def.slot, title: def.title }) : def;
        });

        container.innerHTML = cardsData.map(t => {
          const keyPrefix = t.slot;
          const st = t.status || 'idle';
          const pct = st === 'idle' ? 0 : Math.min(100, Math.max(8, Number(t.percent || 0)));
          const badgeText = st === 'running' ? ('进行中 ' + pct + '%') : (st === 'done' ? '已完成 100%' : (st === 'error' ? '异常' : '待机中'));
          const timeMeta = t.updated_at ? `${t.updated_at} · ${t.elapsed_s || 0}s` : '等待触发';
          const stepsText = (t.steps || []).map(s => `[${s.time}] (${s.percent}%) ${s.stage}`).join('\n');
          const outCombined = (t.output_text ? t.output_text + (stepsText ? '\n━━━━━━━━━━━━━━\n' : '') : '') + stepsText;

          return `
            <div class="rt-card">
              <div class="flex items-center justify-between gap-2">
                <div class="font-bold text-xs text-slate-100">${t.title}</div>
                <div class="flex items-center gap-1.5">
                  <span class="text-[11px] font-mono text-slate-400">${timeMeta}</span>
                  <span class="rt-badge ${st}">${badgeText}</span>
                </div>
              </div>
              <div class="text-xs text-sky-300 font-mono truncate" title="${(t.stage || '').replace(/"/g, '&quot;')}">${t.stage || '-'}</div>
              <div class="rt-bar-bg"><div class="rt-bar-fill" style="width:${pct}%;"></div></div>
              <div class="text-[11px] text-slate-400 font-mono truncate">${t.source ? t.source + ' | ' : ''}${t.extra_info || ''}</div>

              <div class="rt-field-label">
                <span>📥 接收信息</span>
                <span class="font-mono">${(t.input_text || '').length} 字</span>
              </div>
              <textarea class="rt-textarea" data-box-key="${keyPrefix}_in" readonly placeholder="尚无接收信息..."></textarea>

              <div class="rt-field-label">
                <span>🧠 完整生图/声学 Prompt</span>
                <span class="font-mono">${(t.prompt_text || '').length} 字</span>
              </div>
              <textarea class="rt-textarea" data-box-key="${keyPrefix}_prompt" readonly placeholder="尚无重写/声学 Prompt..."></textarea>

              <div class="rt-field-label">
                <span>📤 输出结果与阶段流水</span>
                <span class="font-mono">${outCombined.length} 字</span>
              </div>
              <textarea class="rt-textarea" data-box-key="${keyPrefix}_out" readonly placeholder="尚无输出结果与阶段流水..."></textarea>
            </div>
          `;
        }).join('');

        // 安全写入文本内容（防止 Prompt 中的特殊 HTML 符号破坏 DOM）
        cardsData.forEach(t => {
          const keyPrefix = t.slot;
          const stepsText = (t.steps || []).map(s => `[${s.time}] (${s.percent}%) ${s.stage}`).join('\n');
          const outCombined = (t.output_text ? t.output_text + (stepsText ? '\n━━━━━━━━━━━━━━\n' : '') : '') + stepsText;
          const elIn = container.querySelector(`textarea[data-box-key="${keyPrefix}_in"]`);
          const elPr = container.querySelector(`textarea[data-box-key="${keyPrefix}_prompt"]`);
          const elOut = container.querySelector(`textarea[data-box-key="${keyPrefix}_out"]`);
          if (elIn) elIn.value = t.input_text || '';
          if (elPr) elPr.value = t.prompt_text || '';
          if (elOut) elOut.value = outCombined || '';
        });

        bindResizeObserver(container);
      }

      async function pollProgress() {
        try {
          const r = await fetch('/api/progress');
          if (!r.ok) return;
          const data = await r.json();
          renderCards(data.tasks || []);
        } catch (e) {
          // 忽略瞬时网络抖动
        }
      }

      renderCards([]);
      pollProgress();
      setInterval(pollProgress, 1200);
    })();
  </script>

</body>
</html>
"""

PANEL_HTML = _DynamicPanelHtml(_RAW_PANEL_HTML)
