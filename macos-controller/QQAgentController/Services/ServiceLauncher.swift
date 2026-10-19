import Foundation
import AppKit

@MainActor
final class ServiceLauncher: ObservableObject {
    static let shared = ServiceLauncher()

    @Published var isBackendActive: Bool = false
    @Published var isQQActive: Bool = false
    @Published var statusMessage: String = ""
    @Published var isLaunching: Bool = false

    private let workspacePath = "/Users/daijinglin/Desktop/qq-agent-workspace"
    private let pythonPath = "/Users/daijinglin/Desktop/qq-agent-workspace/bot/.venv/bin/python"
    private let daemonScriptPath = "/Users/daijinglin/Desktop/qq-agent-workspace/bot/daemon_launcher.py"
    private let logPath = "/Users/daijinglin/Desktop/qq-agent-workspace/bot/bot.restart.log"
    private let qqPath = "/Applications/QQ.app"

    private init() {
        refreshStatuses()
    }

    func refreshStatuses() {
        let qqRunning = checkQQRunning()
        if isQQActive != qqRunning {
            isQQActive = qqRunning
        }
    }

    // MARK: - 后端检测与启动

    func checkBackendRunning() async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:8080/api/status") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.0
        let config = URLSessionConfiguration.ephemeral
        config.connectionProxyDictionary = [:]
        let directSession = URLSession(configuration: config)
        do {
            let (_, response) = try await directSession.data(for: request)
            if let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) {
                return true
            }
        } catch {
            return false
        }
        return false
    }

    func startBackend() async -> (success: Bool, message: String) {
        if await checkBackendRunning() {
            isBackendActive = true
            return (true, "后端已在运行中 (8080)")
        }

        guard FileManager.default.fileExists(atPath: daemonScriptPath) else {
            return (false, "找不到 daemon_launcher.py 启动器")
        }

        // 使用双 fork + setsid 的 Python 守护脚本拉起后端，永久脱离 App 进程
        let execPython = FileManager.default.fileExists(atPath: pythonPath) ? pythonPath : "/usr/bin/python3"
        let cmd = "'\(execPython)' '\(daemonScriptPath)'"
        _ = runShellCommand(cmd)

        // 轮询等待后端启动就绪（最多 8 秒）
        for _ in 0..<16 {
            try? await Task.sleep(nanoseconds: 500_000_000)
            if await checkBackendRunning() {
                isBackendActive = true
                return (true, "NoneBot 后端常驻启动成功 (8080)")
            }
        }

        isBackendActive = false
        return (false, "后端已拉起但未响应 8080 端口，请查看下方实时日志")
    }

    func restartBackend() async -> (success: Bool, message: String) {
        // 先终止已存在的旧 bot 进程，彻底避免 8080 端口占用或 404
        _ = runShellCommand("pkill -f 'python.*bot.py'")
        try? await Task.sleep(nanoseconds: 500_000_000)
        isBackendActive = false
        return await startBackend()
    }

    // MARK: - NapCat QQ 检测与启动

    func checkQQRunning() -> Bool {
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: "com.tencent.qq")
        if !apps.isEmpty { return true }
        // 兜底检查无头/隐藏模式下的 QQ.app 二进制进程
        let pids = runShellCommand("pgrep -f 'QQ.app/Contents/MacOS/QQ'")
        return !pids.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// 打开交互式终端以显示二维码供手机 QQ 扫码登录
    func openTerminalForNapCatLogin() -> (success: Bool, message: String) {
        guard FileManager.default.fileExists(atPath: qqPath) else {
            return (false, "未在 /Applications/QQ.app 找到 QQ 客户端")
        }

        let appleScriptContent = """
        tell application "Terminal"
            activate
            do script "echo '=== 正在启动 NapCat 登录终端 (请准备手机 QQ 扫码) ===\\n'; '/Applications/QQ.app/Contents/MacOS/QQ' --no-sandbox"
        end tell
        """

        var error: NSDictionary?
        if let script = NSAppleScript(source: appleScriptContent) {
            script.executeAndReturnError(&error)
            if error == nil {
                return (true, "已在终端打开 NapCat 登录窗口，请用手机扫码")
            }
        }
        return (false, "拉起终端扫码失败")
    }

    /// 登录后无界面后台运行 QQ（由终端/控制面板代理收发）
    func startNapCatQQ() -> (success: Bool, message: String) {
        if checkQQRunning() {
            isQQActive = true
            return (true, "QQ 已在运行中")
        }

        guard FileManager.default.fileExists(atPath: qqPath) else {
            return (false, "未在 /Applications/QQ.app 找到 QQ 客户端")
        }

        // 以 NapCat --no-sandbox 模式静默拉起 QQ（-g 保持后台不置顶，-j 隐藏启动窗口，所有对话由终端/控制面板代理）
        let cmd = "open -g -j -a '\(qqPath)' --args --no-sandbox"
        _ = runShellCommand(cmd)

        // 延时检测进程
        Thread.sleep(forTimeInterval: 0.8)
        isQQActive = checkQQRunning()
        if isQQActive {
            return (true, "NapCat QQ 已静默启动 (后台运行中)")
        } else {
            return (true, "QQ 静默启动命令已发出")
        }
    }

    // MARK: - 整合自动启动

    func autoLaunchAll() async {
        isLaunching = true
        statusMessage = "正在初始化服务环境…"

        // 1. 自动拉起后端
        let backendRunning = await checkBackendRunning()
        if !backendRunning {
            statusMessage = "正在启动 8080 后端…"
            let res = await startBackend()
            statusMessage = res.message
        } else {
            isBackendActive = true
        }

        // 2. 自动拉起 NapCat QQ
        let qqRunning = checkQQRunning()
        if !qqRunning {
            statusMessage = "正在拉起 NapCat 模式 QQ…"
            let res = startNapCatQQ()
            statusMessage = res.message
        } else {
            isQQActive = true
        }

        isLaunching = false
        statusMessage = "自动启动检查完成"
    }

    // MARK: - Shell 工具函数

    @discardableResult
    private func runShellCommand(_ command: String) -> String {
        let process = Process()
        let pipe = Pipe()

        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-c", command]
        process.standardOutput = pipe
        process.standardError = pipe

        do {
            try process.run()
            process.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            return String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        } catch {
            return "执行失败: \(error.localizedDescription)"
        }
    }
}
