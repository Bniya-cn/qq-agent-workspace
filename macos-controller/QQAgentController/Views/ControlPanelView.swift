import AppKit
import SwiftUI

enum WorkspaceTab: String, CaseIterable, Identifiable {
    case bot = "bot"
    case voice = "voice"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .bot: return "机器人模式"
        case .voice: return "语音模式"
        }
    }

    var subtitle: String {
        switch self {
        case .bot: return "对话 · 生图 · 群管"
        case .voice: return "SoVITS · 声学调参"
        }
    }

    var icon: String {
        switch self {
        case .bot: return "cpu.fill"
        case .voice: return "waveform.badge.mic"
        }
    }

    var accentColor: Color {
        switch self {
        case .bot:
            return Color(red: 14 / 255, green: 165 / 255, blue: 233 / 255) // #0EA5E9 Sky/Cobalt
        case .voice:
            return Color(red: 16 / 255, green: 185 / 255, blue: 129 / 255) // #10B981 Acoustic Emerald
        }
    }
}

struct ControlPanelView: View {
    @ObservedObject var api: AgentAPIService
    @ObservedObject private var launcher = ServiceLauncher.shared
    @State private var selectedTab: WorkspaceTab = .bot
    @State private var previewText: String = "哼，终于想起找我啦？今天的数据报告我已经帮你整理好放在桌上了，别忘了看！"

    private let bgCanvas = Color(red: 9 / 255, green: 10 / 255, blue: 15 / 255)
    private let bgSurface = Color(red: 16 / 255, green: 18 / 255, blue: 26 / 255)
    private let bgElevated = Color(red: 23 / 255, green: 26 / 255, blue: 38 / 255)
    private let borderSubtle = Color.white.opacity(0.08)

    var body: some View {
        VStack(spacing: 0) {
            topHeaderBar
            Divider().overlay(borderSubtle)

            ScrollView(.vertical, showsIndicators: true) {
                VStack(alignment: .leading, spacing: 14) {
                    if selectedTab == .bot {
                        botWorkspaceContent
                    } else {
                        voiceWorkspaceContent
                    }

                    statusFooterBar
                }
                .padding(14)
            }
        }
        .background(bgCanvas)
        .preferredColorScheme(.dark)
        .frame(minWidth: 480, maxWidth: .infinity, minHeight: 680, maxHeight: .infinity)
    }

    // MARK: - Top Header & Dual-Mode Segmented Switcher

    private var topHeaderBar: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                ZStack {
                    RoundedRectangle(cornerRadius: 9, style: .continuous)
                        .fill(selectedTab.accentColor.opacity(0.18))
                        .frame(width: 34, height: 34)
                        .overlay(
                            RoundedRectangle(cornerRadius: 9, style: .continuous)
                                .stroke(selectedTab.accentColor.opacity(0.45), lineWidth: 1)
                        )
                    Image(systemName: selectedTab.icon)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(selectedTab.accentColor)
                }

                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text("PRTS · 罗德岛神经中枢")
                            .font(.system(size: 13, weight: .bold))
                            .foregroundStyle(.white)
                        statusPill
                    }
                    Text(statusSubtitle)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }

                Spacer()

                Button {
                    if let url = URL(string: "http://127.0.0.1:8080/panel") {
                        NSWorkspace.shared.open(url)
                    }
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "safari.fill")
                        Text("网页工作台")
                    }
                    .font(.system(size: 11, weight: .medium))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 5)
                    .background(bgElevated)
                    .foregroundStyle(.white.opacity(0.9))
                    .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 6, style: .continuous)
                            .stroke(borderSubtle, lineWidth: 1)
                    )
                }
                .buttonStyle(.plain)
            }

            // Segmented Switcher: 机器人模式 vs 语音模式
            HStack(spacing: 6) {
                ForEach(WorkspaceTab.allCases) { tab in
                    let isSelected = (selectedTab == tab)
                    Button {
                        withAnimation(.spring(response: 0.25, dampingFraction: 0.82)) {
                            selectedTab = tab
                        }
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: tab.icon)
                                .font(.system(size: 12, weight: .semibold))
                                .foregroundStyle(isSelected ? tab.accentColor : .secondary)
                            VStack(alignment: .leading, spacing: 1) {
                                Text(tab.title)
                                    .font(.system(size: 12, weight: .bold))
                                    .foregroundStyle(isSelected ? .white : .white.opacity(0.65))
                                Text(tab.subtitle)
                                    .font(.system(size: 9))
                                    .foregroundStyle(isSelected ? tab.accentColor.opacity(0.95) : .secondary)
                            }
                            Spacer(minLength: 0)
                            Circle()
                                .fill(tab == .bot ? (api.globalActive ? tab.accentColor : .gray.opacity(0.4)) : (api.voiceEnabled ? tab.accentColor : .gray.opacity(0.4)))
                                .frame(width: 7, height: 7)
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 7)
                        .background(isSelected ? bgElevated : Color.clear)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                        .overlay(
                            RoundedRectangle(cornerRadius: 8, style: .continuous)
                                .stroke(isSelected ? tab.accentColor.opacity(0.55) : Color.clear, lineWidth: 1)
                        )
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(4)
            .background(bgSurface)
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .stroke(borderSubtle, lineWidth: 1)
            )
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(bgSurface.opacity(0.85))
    }

    // MARK: - Tab 1: 🤖 机器人模式 (Bot Mode Workspace)

    private var botWorkspaceContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            // 1. 链路状态与进程控制卡片
            cardContainer(title: "系统链路与 NapCat 守护", icon: "bolt.horizontal.circle.fill", accent: WorkspaceTab.bot.accentColor) {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 8) {
                        Circle()
                            .fill(api.connected ? Color.green : Color.red)
                            .frame(width: 8, height: 8)
                        Text(api.connected ? "NoneBot 8080 引擎在线" : "NoneBot 8080 未响应")
                            .font(.system(size: 11, weight: .medium))
                        Spacer()
                        if !api.connected {
                            actionChip("启动后端", icon: "play.fill", tint: WorkspaceTab.bot.accentColor) {
                                Task {
                                    _ = await launcher.startBackend()
                                    await api.refreshStatus()
                                }
                            }
                        } else {
                            actionChip("重启后端", icon: "arrow.clockwise", tint: .secondary) {
                                Task {
                                    _ = await launcher.restartBackend()
                                    await api.refreshStatus()
                                }
                            }
                        }
                    }

                    HStack(spacing: 8) {
                        Circle()
                            .fill(launcher.isQQActive ? Color.green : Color.orange)
                            .frame(width: 8, height: 8)
                        Text(launcher.isQQActive ? "QQ (NapCat 无沙盒) 运行中" : "QQ 未运行")
                            .font(.system(size: 11, weight: .medium))
                        Spacer()
                        actionChip("终端扫码", icon: "qrcode", tint: WorkspaceTab.bot.accentColor) {
                            _ = launcher.openTerminalForNapCatLogin()
                        }
                        if !launcher.isQQActive {
                            actionChip("静默拉起", icon: "paperplane.fill", tint: .secondary) {
                                _ = launcher.startNapCatQQ()
                            }
                        }
                    }

                    Divider().overlay(borderSubtle)

                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("机器人全局响应总开关")
                                .font(.system(size: 12, weight: .semibold))
                            Text("关闭后仅响应管理员 /on 与状态指令，忽略普通群消息")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Toggle("", isOn: Binding(
                            get: { api.globalActive },
                            set: { api.setGlobalSwitch($0) }
                        ))
                        .toggleStyle(.switch)
                        .labelsHidden()
                    }
                }
            }

            // 2. 干员人设矩阵卡片
            cardContainer(title: "干员人设与 RAG 路由", icon: "person.crop.square.filled.and.at.rectangle", accent: WorkspaceTab.bot.accentColor) {
                VStack(alignment: .leading, spacing: 8) {
                    let characters: [(id: String, name: String, tag: String, desc: String)] = [
                        ("amiya", "阿米娅", "Amiya · 百炼 RAG", "罗德岛领袖，温柔体贴，支持联网问答与 2D 立绘生图"),
                        ("kaltsit", "凯尔希", "Kal'tsit · 医疗部", "冷静简练，逻辑严密，精准解答专业问题"),
                        ("dania", "达妮娅", "Dania · 鸣潮拉海洛", "傲娇嘴硬但关心同伴，搭配 GPT-SoVITS 专属语音库"),
                        ("none", "群聊复刻", "Group Persona", "基于拖入的 .txt 群聊日志自动提取群友黑话与性格")
                    ]

                    LazyVGrid(columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)], spacing: 8) {
                        ForEach(characters, id: \.id) { item in
                            let active = (api.activeCharacter == item.id)
                            Button {
                                api.setCharacter(item.id)
                            } label: {
                                VStack(alignment: .leading, spacing: 4) {
                                    HStack {
                                        Text(item.name)
                                            .font(.system(size: 12, weight: .bold))
                                            .foregroundStyle(active ? .white : .white.opacity(0.85))
                                        Spacer()
                                        Text(item.tag)
                                            .font(.system(size: 9, design: .monospaced))
                                            .foregroundStyle(active ? WorkspaceTab.bot.accentColor : .secondary)
                                    }
                                    Text(item.desc)
                                        .font(.system(size: 10))
                                        .foregroundStyle(.secondary)
                                        .lineLimit(2)
                                        .multilineTextAlignment(.leading)
                                }
                                .padding(9)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(active ? WorkspaceTab.bot.accentColor.opacity(0.14) : bgElevated.opacity(0.7))
                                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                                .overlay(
                                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                                        .stroke(active ? WorkspaceTab.bot.accentColor.opacity(0.75) : borderSubtle, lineWidth: 1)
                                )
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }

            // 3. 白名单群与安慰模式管理
            cardContainer(title: "白名单群聊 & 私聊安慰名单", icon: "person.2.wave.2.fill", accent: WorkspaceTab.bot.accentColor) {
                VStack(alignment: .leading, spacing: 10) {
                    // 群白名单
                    VStack(alignment: .leading, spacing: 6) {
                        Text("允许触发的 QQ 群白名单")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.white.opacity(0.9))

                        HStack(spacing: 6) {
                            TextField("输入群号 (如 130439695)", text: $api.targetGroup)
                                .textFieldStyle(.roundedBorder)
                                .font(.system(size: 11, design: .monospaced))
                            actionChip("加入白名单", icon: "plus", tint: WorkspaceTab.bot.accentColor) {
                                api.toggleGroup(groupId: api.targetGroup, enable: true)
                            }
                        }

                        if api.activeGroups.isEmpty {
                            Text("暂未配置白名单群")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        } else {
                            FlowPillList(items: api.activeGroups, prefix: "群", tint: WorkspaceTab.bot.accentColor) { gid in
                                api.toggleGroup(groupId: gid, enable: false)
                            }
                        }
                    }

                    Divider().overlay(borderSubtle)

                    // 私聊安慰目标
                    VStack(alignment: .leading, spacing: 6) {
                        Text("私聊主动共情安慰 QQ 名单")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.white.opacity(0.9))

                        HStack(spacing: 6) {
                            TextField("输入好友 QQ 号开启专属安慰", text: $api.targetQQ)
                                .textFieldStyle(.roundedBorder)
                                .font(.system(size: 11, design: .monospaced))
                            actionChip("添加目标", icon: "heart.fill", tint: WorkspaceTab.bot.accentColor) {
                                api.toggleComfort(target: api.targetQQ, enable: true)
                            }
                        }

                        if api.comfortTargets.isEmpty {
                            Text("暂未绑定安慰目标")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        } else {
                            FlowPillList(items: api.comfortTargets, prefix: "QQ", tint: .pink) { uid in
                                api.toggleComfort(target: uid, enable: false)
                            }
                        }
                    }
                }
            }

            // 4. 视觉立绘库与群画像提取
            cardContainer(title: "角色立绘参考库 & 群记录提取", icon: "photo.stack.fill", accent: WorkspaceTab.bot.accentColor) {
                VStack(alignment: .leading, spacing: 8) {
                    if !api.characterRefs.isEmpty {
                        HStack(alignment: .top, spacing: 6) {
                            Text("已归档角色立绘:")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(.secondary)
                            Text(api.characterRefs.joined(separator: " · "))
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(WorkspaceTab.bot.accentColor)
                        }
                    }

                    ZStack {
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .fill(bgElevated.opacity(0.5))
                            .overlay(
                                RoundedRectangle(cornerRadius: 8, style: .continuous)
                                    .stroke(style: StrokeStyle(lineWidth: 1, dash: [4]))
                                    .foregroundStyle(WorkspaceTab.bot.accentColor.opacity(0.45))
                            )
                        HStack(spacing: 6) {
                            Image(systemName: "doc.badge.plus")
                                .foregroundStyle(WorkspaceTab.bot.accentColor)
                            Text("拖拽群聊天记录 .txt 到此自动提取群画像与 Few-Shot 语料")
                                .font(.system(size: 11))
                                .foregroundStyle(.white.opacity(0.8))
                        }
                    }
                    .frame(height: 44)
                    .onDrop(of: [.fileURL], isTargeted: nil) { providers in
                        api.handleFileDrop(providers)
                    }
                }
            }

            // 5. 实时对话流与日志终端
            dialogueAndLogSection
        }
    }

    // MARK: - Tab 2: 🎙️ 语音模式 (Voice Mode Workspace)

    private var voiceWorkspaceContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            // 1. GPT-SoVITS 双引擎状态矩阵 (9880 推理服务 + 9876 训练工作台)
            cardContainer(title: "GPT-SoVITS 语音引擎矩阵", icon: "server.rack", accent: WorkspaceTab.voice.accentColor) {
                VStack(alignment: .leading, spacing: 10) {
                    // 9880 推理引擎
                    HStack(spacing: 8) {
                        Circle()
                            .fill(api.api9880Running ? WorkspaceTab.voice.accentColor : Color.red)
                            .frame(width: 8, height: 8)
                        VStack(alignment: .leading, spacing: 1) {
                            Text("TTS 推理引擎 (:9880)")
                                .font(.system(size: 11, weight: .bold))
                            Text(api.api9880Running ? "MPS 加速推理就绪 · 实时合成已连接" : "未运行 · 点击右侧一键拉起")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        if api.api9880Running {
                            actionChip("重启 9880", icon: "arrow.clockwise", tint: .secondary) {
                                api.controlVoiceService(service: "api_9880", action: "start")
                            }
                            actionChip("停止", icon: "stop.fill", tint: .red.opacity(0.8)) {
                                api.controlVoiceService(service: "api_9880", action: "stop")
                            }
                        } else {
                            actionChip("启动 9880", icon: "play.fill", tint: WorkspaceTab.voice.accentColor) {
                                api.controlVoiceService(service: "api_9880", action: "start")
                            }
                        }
                    }

                    Divider().overlay(borderSubtle)

                    // 9876 训练工作台
                    HStack(spacing: 8) {
                        Circle()
                            .fill(api.studio9876Running ? WorkspaceTab.voice.accentColor : Color.gray)
                            .frame(width: 8, height: 8)
                        VStack(alignment: .leading, spacing: 1) {
                            Text("Voice Studio 训练工作台 (:9876)")
                                .font(.system(size: 11, weight: .bold))
                            Text(api.studio9876Running ? "WebUI 运行中 · 支持切分与角色绑定" : "按需启动 · 管理参考音频与微调")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        if api.studio9876Running {
                            actionChip("打开 9876", icon: "arrow.up.right.square", tint: WorkspaceTab.voice.accentColor) {
                                if let url = URL(string: "http://127.0.0.1:9876") {
                                    NSWorkspace.shared.open(url)
                                }
                            }
                            actionChip("停止", icon: "stop.fill", tint: .red.opacity(0.8)) {
                                api.controlVoiceService(service: "studio_9876", action: "stop")
                            }
                        } else {
                            actionChip("启动 9876", icon: "play.fill", tint: WorkspaceTab.voice.accentColor) {
                                api.controlVoiceService(service: "studio_9876", action: "start")
                            }
                        }
                    }
                }
            }

            // 2. 语音回复策略与情绪路由
            cardContainer(title: "语音发送策略与情感路由", icon: "Slider.horizontal.below.waveform", accent: WorkspaceTab.voice.accentColor) {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("启用 QQ 语音合成回复 (TTS)")
                                .font(.system(size: 12, weight: .semibold))
                            Text("开启后机器人会在回复时调用 9880 引擎生成角色语音条")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Toggle("", isOn: Binding(
                            get: { api.voiceEnabled },
                            set: { api.updateVoiceConfig(ttsEnabled: $0) }
                        ))
                        .toggleStyle(.switch)
                        .labelsHidden()
                    }

                    Divider().overlay(borderSubtle)

                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("纯语音条沉浸模式 (不发文字气泡)")
                                .font(.system(size: 12, weight: .semibold))
                            Text("开启后短句回复仅发送语音条（若包含图片、长文或合成超时则自动回退文字）")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Toggle("", isOn: Binding(
                            get: { api.voiceOnlyReply },
                            set: { api.updateVoiceConfig(voiceOnlyReply: $0) }
                        ))
                        .toggleStyle(.switch)
                        .labelsHidden()
                    }

                    Divider().overlay(borderSubtle)

                    Picker("语音模型", selection: Binding(
                        get: { api.voiceModel },
                        set: { api.updateVoiceConfig(voiceModel: $0) }
                    )) {
                        Text("跟随当前角色").tag("auto")
                        ForEach(api.voiceModels.filter { $0.ready }) { model in
                            Text(model.name).tag(model.id)
                        }
                    }

                    VStack(alignment: .leading, spacing: 6) {
                        Text("AI 情绪路由")
                            .font(.system(size: 11, weight: .semibold))
                        HStack(spacing: 8) {
                            modeSelectButton(
                                title: "按完整回复选择情绪",
                                subtitle: "AI 选择当前模型的参考音频；普通称呼不会触发睡前音色",
                                active: true
                            ) {
                                api.updateVoiceConfig(voiceMode: "emotion_auto")
                            }
                        }
                    }

                    if !api.voiceRefs.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("当前可用参考音频条目 (\(api.voiceRefs.count) 条)")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(.secondary)

                            ScrollView(.horizontal, showsIndicators: false) {
                                HStack(spacing: 6) {
                                    ForEach(api.voiceRefs) { ref in
                                        let selected = (api.voiceRefName == ref.name)
                                        Button {
                                            api.previewVoice(text: previewText, refName: ref.name)
                                        } label: {
                                            HStack(spacing: 4) {
                                                Image(systemName: selected ? "checkmark.circle.fill" : "waveform")
                                                    .font(.system(size: 10))
                                                Text("\(ref.character)/\(ref.name)")
                                                    .font(.system(size: 10, design: .monospaced))
                                            }
                                            .padding(.horizontal, 8)
                                            .padding(.vertical, 5)
                                            .background(selected ? WorkspaceTab.voice.accentColor.opacity(0.22) : bgElevated)
                                            .foregroundStyle(selected ? WorkspaceTab.voice.accentColor : .white.opacity(0.8))
                                            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                                            .overlay(
                                                RoundedRectangle(cornerRadius: 6, style: .continuous)
                                                    .stroke(selected ? WorkspaceTab.voice.accentColor : borderSubtle, lineWidth: 1)
                                            )
                                        }
                                        .buttonStyle(.plain)
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // 3. 声学参数调校滑块 (Speed / Temperature / Top-P)
            cardContainer(title: "声学合成参数调校", icon: "dial.medium.fill", accent: WorkspaceTab.voice.accentColor) {
                VStack(alignment: .leading, spacing: 10) {
                    acousticSliderRow(
                        label: "语速倍率 (Speed Factor)",
                        value: $api.voiceSpeed,
                        range: 0.6...1.6,
                        step: 0.05,
                        unit: "x"
                    ) { val in
                        api.updateVoiceConfig(voiceSpeed: val)
                    }

                    acousticSliderRow(
                        label: "采样温度 (Temperature · 情感起伏)",
                        value: $api.voiceTemperature,
                        range: 0.1...1.5,
                        step: 0.05,
                        unit: ""
                    ) { val in
                        api.updateVoiceConfig(voiceTemperature: val)
                    }

                    acousticSliderRow(
                        label: "核采样阈值 (Top-P · 咬字稳定性)",
                        value: $api.voiceTopP,
                        range: 0.1...1.0,
                        step: 0.05,
                        unit: ""
                    ) { val in
                        api.updateVoiceConfig(voiceTopP: val)
                    }
                }
            }

            // 4. 在线实时试听间
            cardContainer(title: "实时语音合成试听间", icon: "headphones", accent: WorkspaceTab.voice.accentColor) {
                VStack(alignment: .leading, spacing: 8) {
                    TextField("输入任意台词立即试听当前声学配置...", text: $previewText, axis: .vertical)
                        .lineLimit(2...4)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 11))

                    HStack {
                        if let statusMsg = api.previewStatusMessage {
                            Text(statusMsg)
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(WorkspaceTab.voice.accentColor)
                                .lineLimit(1)
                        } else {
                            Text("支持随时校验语速、采样温度与情绪路由效果")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        actionChip(
                            api.isPreviewingVoice ? "合成中..." : "立即合成试听",
                            icon: "play.circle.fill",
                            tint: WorkspaceTab.voice.accentColor
                        ) {
                            api.previewVoice(text: previewText)
                        }
                        .disabled(api.isPreviewingVoice)
                    }
                }
            }
        }
    }

    // MARK: - Shared Dialogue & Log Section

    private var dialogueAndLogSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            cardContainer(title: "AI 最新对话实况", icon: "bubble.left.and.bubble.right.fill", accent: WorkspaceTab.bot.accentColor) {
                if api.recentDialogues.isEmpty {
                    Text("暂无对话记录（在白名单群里 @机器人 或私聊即可触发）")
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 4)
                } else {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(api.recentDialogues.suffix(3)) { d in
                            VStack(alignment: .leading, spacing: 4) {
                                HStack {
                                    Text(d.source)
                                        .font(.system(size: 9, design: .monospaced))
                                        .padding(.horizontal, 5)
                                        .padding(.vertical, 2)
                                        .background(WorkspaceTab.bot.accentColor.opacity(0.18))
                                        .foregroundStyle(WorkspaceTab.bot.accentColor)
                                        .clipShape(RoundedRectangle(cornerRadius: 4))
                                    Text(d.userText)
                                        .font(.system(size: 11, weight: .medium))
                                        .foregroundStyle(.white.opacity(0.9))
                                        .lineLimit(2)
                                    Spacer()
                                    if d.voice == true {
                                        Image(systemName: "waveform")
                                            .font(.system(size: 10))
                                            .foregroundStyle(WorkspaceTab.voice.accentColor)
                                    }
                                    Text(d.time)
                                        .font(.system(size: 9, design: .monospaced))
                                        .foregroundStyle(.secondary)
                                }
                                HStack(alignment: .top, spacing: 5) {
                                    Text("\(d.character):")
                                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                                        .foregroundStyle(WorkspaceTab.bot.accentColor)
                                    Text(d.reply)
                                        .font(.system(size: 11))
                                        .foregroundStyle(.white.opacity(0.82))
                                        .lineLimit(3)
                                }
                            }
                            .padding(8)
                            .background(bgElevated.opacity(0.75))
                            .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))
                        }
                    }
                }
            }

            cardContainer(title: "后端实时事件流 (\(api.recentLogs.count) 条)", icon: "terminal.fill", accent: .secondary) {
                ScrollView([.vertical, .horizontal], showsIndicators: true) {
                    VStack(alignment: .leading, spacing: 3) {
                        if api.recentLogs.isEmpty {
                            Text("等待事件接入…")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.secondary)
                        } else {
                            ForEach(api.recentLogs.suffix(15)) { item in
                                Text("[\(item.time)] \(item.text)")
                                    .font(.system(size: 10, design: .monospaced))
                                    .foregroundStyle(logColor(for: item.level))
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    .padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(minHeight: 100, maxHeight: 180)
                .background(bgCanvas.opacity(0.8))
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            }
        }
    }

    // MARK: - Footer Bar

    private var statusFooterBar: some View {
        HStack {
            if let msg = api.extractMessage {
                Text(msg)
                    .font(.system(size: 10))
                    .foregroundStyle(WorkspaceTab.bot.accentColor)
            } else if let err = api.lastError {
                Text(err)
                    .font(.system(size: 10))
                    .foregroundStyle(.red)
            } else if !launcher.statusMessage.isEmpty {
                Text(launcher.statusMessage)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            } else {
                Text("Web 控制台地址: http://127.0.0.1:8080/panel")
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button("退出控制器", role: .destructive) {
                NSApplication.shared.terminate(nil)
            }
            .controlSize(.small)
        }
        .padding(.top, 2)
    }

    // MARK: - Reusable UI Components

    private func cardContainer<Content: View>(
        title: String,
        icon: String,
        accent: Color,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(accent)
                Text(title)
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(.white.opacity(0.92))
                Spacer()
            }
            content()
        }
        .padding(12)
        .background(bgSurface)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(borderSubtle, lineWidth: 1)
        )
    }

    private func actionChip(_ label: String, icon: String, tint: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Image(systemName: icon)
                    .font(.system(size: 10, weight: .semibold))
                Text(label)
                    .font(.system(size: 10, weight: .semibold))
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(tint.opacity(0.18))
            .foregroundStyle(tint == .secondary ? .white.opacity(0.85) : tint)
            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .stroke(tint.opacity(0.4), lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
    }

    private func modeSelectButton(title: String, subtitle: String, active: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text(title)
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(active ? .white : .white.opacity(0.8))
                    Spacer()
                    Image(systemName: active ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 11))
                        .foregroundStyle(active ? WorkspaceTab.voice.accentColor : .secondary)
                }
                Text(subtitle)
                    .font(.system(size: 9))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
            }
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(active ? WorkspaceTab.voice.accentColor.opacity(0.14) : bgElevated.opacity(0.6))
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(active ? WorkspaceTab.voice.accentColor.opacity(0.75) : borderSubtle, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
    }

    private func acousticSliderRow(
        label: String,
        value: Binding<Double>,
        range: ClosedRange<Double>,
        step: Double,
        unit: String,
        onCommit: @escaping (Double) -> Void
    ) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(label)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.white.opacity(0.88))
                Spacer()
                Text(String(format: "%.2f%@", value.wrappedValue, unit))
                    .font(.system(size: 11, weight: .bold, design: .monospaced))
                    .foregroundStyle(WorkspaceTab.voice.accentColor)
            }
            Slider(value: value, in: range, step: step) { editing in
                if !editing {
                    onCommit(value.wrappedValue)
                }
            }
            .tint(WorkspaceTab.voice.accentColor)
        }
    }

    private var statusPill: some View {
        HStack(spacing: 4) {
            Circle()
                .fill(statusColor)
                .frame(width: 6, height: 6)
            Text(api.connected && api.botConnected ? "ONLINE" : (api.connected ? "STANDBY" : "OFFLINE"))
                .font(.system(size: 9, weight: .bold, design: .monospaced))
                .foregroundStyle(statusColor)
        }
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(statusColor.opacity(0.14))
        .clipShape(Capsule())
    }

    private var statusColor: Color {
        if !api.connected { return .red }
        if !api.botConnected { return .orange }
        return WorkspaceTab.voice.accentColor
    }

    private var statusSubtitle: String {
        if !api.connected {
            return "8080 离线 · 请点击启动后端"
        }
        if !api.botConnected {
            return "8080 在线 · 等待 NapCat QQ 接入"
        }
        let botId = api.connectedBots.first ?? "已连接"
        return "Bot ID: \(botId) · 角色: \(api.activeCharacter.uppercased()) · TTS: \(api.voiceEnabled ? "ON" : "OFF")"
    }

    private func logColor(for level: String?) -> Color {
        switch level {
        case "WARN": return .orange
        case "ERROR": return .red
        default: return .white.opacity(0.82)
        }
    }
}

struct FlowPillList: View {
    let items: [String]
    let prefix: String
    let tint: Color
    let onRemove: (String) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(items, id: \.self) { item in
                    HStack(spacing: 4) {
                        Text("\(prefix) \(item)")
                            .font(.system(size: 10, design: .monospaced))
                        Button {
                            onRemove(item)
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.system(size: 10))
                                .foregroundStyle(.white.opacity(0.6))
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                    .background(tint.opacity(0.16))
                    .foregroundStyle(.white.opacity(0.9))
                    .clipShape(Capsule())
                    .overlay(
                        Capsule().stroke(tint.opacity(0.4), lineWidth: 1)
                    )
                }
            }
        }
    }
}
