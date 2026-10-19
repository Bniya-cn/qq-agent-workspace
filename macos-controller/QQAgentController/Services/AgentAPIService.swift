import AVFoundation
import Foundation
import SwiftUI
import UniformTypeIdentifiers

@MainActor
final class AgentAPIService: ObservableObject {
    // MARK: - Bot Mode State
    @Published var globalActive = false
    @Published var activeCharacter = "none"
    @Published var comfortTargets: [String] = []
    @Published var activeGroups: [String] = []
    @Published var characterRefs: [String] = []
    @Published var botConnected = false
    @Published var connectedBots: [String] = []
    @Published var recentLogs: [LogItem] = []
    @Published var recentDialogues: [DialogueItem] = []
    @Published var targetQQ = ""
    @Published var targetGroup = ""
    @Published var lastError: String?
    @Published var connected = false
    @Published var extractMessage: String?

    // MARK: - Voice Mode State
    @Published var voiceEnabled = false
    @Published var voiceOnlyReply = false
    @Published var voiceMode = "emotion_auto"
    @Published var voiceRefName = "default"
    @Published var voiceModel = "auto"
    @Published var voiceModels: [VoiceModelItem] = []
    @Published var voiceSpeed: Double = 1.0
    @Published var voiceTemperature: Double = 0.85
    @Published var voiceTopP: Double = 0.90
    @Published var api9880Running = false
    @Published var studio9876Running = false
    @Published var voiceRefs: [VoiceRefItem] = []
    @Published var isPreviewingVoice = false
    @Published var previewStatusMessage: String?

    private let baseURL = URL(string: "http://127.0.0.1:8080")!
    private var pollTask: Task<Void, Never>?
    private var audioPlayer: AVAudioPlayer?
    private var isAdjustingSliders = false

    private lazy var directSession: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]
        config.timeoutIntervalForRequest = 15.0
        config.timeoutIntervalForResource = 60.0
        return URLSession(configuration: config)
    }()

    init() {
        startPolling()
    }

    deinit {
        pollTask?.cancel()
    }

    func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refreshStatus()
                ServiceLauncher.shared.refreshStatuses()
                try? await Task.sleep(nanoseconds: 1_500_000_000)
            }
        }
    }

    func refreshStatus() async {
        do {
            var req = URLRequest(url: baseURL.appendingPathComponent("api/dashboard"))
            req.timeoutInterval = 2.5
            let (data, resp) = try await directSession.data(for: req)
            guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                throw URLError(.badServerResponse)
            }
            let status = try JSONDecoder().decode(AgentStatus.self, from: data)
            globalActive = status.globalActive
            activeCharacter = status.activeCharacter
            activeGroups = status.activeGroups
            comfortTargets = status.comfortTargets
            voiceEnabled = status.ttsEnabled ?? status.voiceEnabled ?? false
            voiceOnlyReply = status.voiceOnlyReply ?? false
            voiceMode = status.voiceMode ?? "emotion_auto"
            voiceRefName = status.voiceRefName ?? "default"
            voiceModel = status.voiceModel ?? "auto"
            voiceModels = status.voiceModels ?? []
            if !isAdjustingSliders {
                voiceSpeed = status.voiceSpeed ?? 1.0
                voiceTemperature = status.voiceTemperature ?? 0.85
                voiceTopP = status.voiceTopP ?? 0.90
            }
            api9880Running = status.voiceServices?.api9880 ?? false
            studio9876Running = status.voiceServices?.studio9876 ?? false
            voiceRefs = status.voiceRefs ?? []
            characterRefs = status.characterRefs ?? []
            botConnected = status.botConnected ?? false
            connectedBots = status.connectedBots ?? []
            recentLogs = status.recentLogs ?? []
            recentDialogues = status.recentDialogues ?? []
            connected = true
            lastError = nil
        } catch {
            connected = false
            botConnected = false
            lastError = "8080 端口未响应或无连接"
            loadLocalFallbackLogs()
        }
    }

    private func loadLocalFallbackLogs() {
        let logPath = "/Users/daijinglin/Desktop/qq-agent-workspace/bot/bot.restart.log"
        guard let content = try? String(contentsOfFile: logPath, encoding: .utf8) else { return }
        let lines = content.components(separatedBy: "\n").filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        let tailLines = lines.suffix(15)
        var fallbackItems: [LogItem] = []
        for line in tailLines {
            fallbackItems.append(LogItem(time: "离线", level: "LOG", text: line))
        }
        self.recentLogs = fallbackItems
    }

    // MARK: - Bot Mode Controls

    func setGlobalSwitch(_ active: Bool) {
        globalActive = active
        Task {
            do {
                try await postJSON(path: "api/switch/global", body: ["active": active])
                await refreshStatus()
            } catch {
                lastError = error.localizedDescription
                await refreshStatus()
            }
        }
    }

    func setCharacter(_ character: String) {
        activeCharacter = character
        Task {
            do {
                try await postJSON(path: "api/switch/character", body: ["character": character])
                await refreshStatus()
            } catch {
                lastError = error.localizedDescription
                await refreshStatus()
            }
        }
    }

    func toggleGroup(groupId: String, enable: Bool) {
        let gid = groupId.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !gid.isEmpty else {
            lastError = "请输入群号"
            return
        }
        Task {
            do {
                try await postJSON(
                    path: "api/switch/group",
                    body: ["group_id": gid, "enable": enable]
                )
                if enable {
                    targetGroup = ""
                }
                await refreshStatus()
            } catch {
                lastError = error.localizedDescription
            }
        }
    }

    func toggleComfort(target: String, enable: Bool) {
        let qq = target.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !qq.isEmpty else {
            lastError = "请输入 QQ 号"
            return
        }
        Task {
            do {
                try await postJSON(
                    path: "api/switch/comfort",
                    body: ["target": qq, "enable": enable]
                )
                if enable {
                    targetQQ = ""
                }
                await refreshStatus()
            } catch {
                lastError = error.localizedDescription
            }
        }
    }

    // MARK: - Voice Mode Controls

    func setVoice(_ enable: Bool) {
        voiceEnabled = enable
        Task {
            do {
                try await postJSON(path: "api/switch/voice", body: ["enable": enable])
                await refreshStatus()
            } catch {
                lastError = error.localizedDescription
                await refreshStatus()
            }
        }
    }

    func updateVoiceConfig(
        ttsEnabled: Bool? = nil,
        voiceOnlyReply: Bool? = nil,
        voiceMode: String? = nil,
        voiceRefName: String? = nil,
        voiceModel: String? = nil,
        voiceSpeed: Double? = nil,
        voiceTemperature: Double? = nil,
        voiceTopP: Double? = nil
    ) {
        var payload: [String: Any] = [:]
        if let v = voiceModel {
            self.voiceModel = v
            payload["voice_model"] = v
        }
        if let v = ttsEnabled {
            self.voiceEnabled = v
            payload["tts_enabled"] = v
        }
        if let v = voiceOnlyReply {
            self.voiceOnlyReply = v
            payload["voice_only_reply"] = v
        }
        if let v = voiceMode {
            self.voiceMode = v
            payload["voice_mode"] = v
        }
        if let v = voiceRefName {
            self.voiceRefName = v
            payload["voice_ref_name"] = v
        }
        if let v = voiceSpeed {
            self.voiceSpeed = v
            payload["voice_speed"] = (v * 100).rounded() / 100
        }
        if let v = voiceTemperature {
            self.voiceTemperature = v
            payload["voice_temperature"] = (v * 100).rounded() / 100
        }
        if let v = voiceTopP {
            self.voiceTopP = v
            payload["voice_top_p"] = (v * 100).rounded() / 100
        }
        Task {
            isAdjustingSliders = true
            defer { isAdjustingSliders = false }
            do {
                try await postJSON(path: "api/voice/config", body: payload)
                await refreshStatus()
            } catch {
                lastError = error.localizedDescription
            }
        }
    }

    func controlVoiceService(service: String, action: String) {
        Task {
            do {
                previewStatusMessage = "正在执行语音服务指令 (\(service) · \(action))..."
                try await postJSON(
                    path: "api/voice/service",
                    body: ["service": service, "action": action]
                )
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                await refreshStatus()
                previewStatusMessage = "语音服务指令已下发"
            } catch {
                lastError = error.localizedDescription
            }
        }
    }

    func previewVoice(text: String, character: String? = nil, refName: String? = nil) {
        let cleanText = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanText.isEmpty else {
            previewStatusMessage = "请输入试听台词"
            return
        }
        isPreviewingVoice = true
        previewStatusMessage = "正在通过 GPT-SoVITS 合成试听语音..."
        Task {
            defer { isPreviewingVoice = false }
            do {
                var req = URLRequest(url: baseURL.appendingPathComponent("api/voice/preview"))
                req.httpMethod = "POST"
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.timeoutInterval = 150
                let body: [String: Any] = [
                    "text": cleanText,
                    "ref_name": refName ?? "",
                ]
                req.httpBody = try JSONSerialization.data(withJSONObject: body)
                let (data, resp) = try await directSession.data(for: req)
                guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                    previewStatusMessage = "试听失败：请确认 9880 语音引擎已启动"
                    return
                }
                let result = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                guard let uri = result?["audio_data_uri"] as? String,
                      let encoded = uri.split(separator: ",", maxSplits: 1).last,
                      let audioData = Data(base64Encoded: String(encoded)) else {
                    previewStatusMessage = result?["message"] as? String ?? "试听未返回有效音频"
                    return
                }
                audioPlayer = try AVAudioPlayer(data: audioData)
                audioPlayer?.prepareToPlay()
                audioPlayer?.play()
                previewStatusMessage = "正在播放试听语音 (\(audioData.count / 1024) KB)"
            } catch {
                previewStatusMessage = "试听异常：\(error.localizedDescription)"
            }
        }
    }

    // MARK: - Group Log Drop

    func handleFileDrop(_ providers: [NSItemProvider]) -> Bool {
        guard let provider = providers.first else { return false }
        provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, error in
            Task { @MainActor in
                if let error {
                    self.lastError = error.localizedDescription
                    return
                }
                guard
                    let data = item as? Data,
                    let url = URL(dataRepresentation: data, relativeTo: nil)
                else {
                    self.lastError = "无法读取拖入的文件"
                    return
                }
                await self.uploadGroupLog(fileURL: url)
            }
        }
        return true
    }

    private func uploadGroupLog(fileURL: URL) async {
        do {
            let raw = try String(contentsOf: fileURL, encoding: .utf8)
            let groupId = fileURL.deletingPathExtension().lastPathComponent
            extractMessage = "正在提取群「\(groupId)」画像..."
            try await postJSON(
                path: "api/extract/group",
                body: ["group_id": groupId, "raw_text": raw]
            )
            extractMessage = "已完成群画像提取：\(groupId)"
            lastError = nil
            await refreshStatus()
        } catch {
            lastError = "群日志提交失败：\(error.localizedDescription)"
        }
    }

    private func postJSON(path: String, body: [String: Any]) async throws {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 30
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (_, resp) = try await directSession.data(for: req)
        guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
    }
}
