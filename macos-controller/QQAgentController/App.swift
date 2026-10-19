import SwiftUI
import AppKit
import UserNotifications

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    static let api = AgentAPIService()
    private var window: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // 显式声明为常规应用，强制在下方 Dock 栏（程序坞）常驻显示应用大图标
        NSApp.setActivationPolicy(.regular)

        DispatchQueue.main.async { self.showControlPanel() }
        sendFirstLaunchNotificationIfNeeded()

        // 启动 App 后自动检测并拉起后端与 NapCat QQ
        Task.detached(priority: .userInitiated) {
            await ServiceLauncher.shared.autoLaunchAll()
            await AppDelegate.api.refreshStatus()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showControlPanel()
        return true
    }

    func showControlPanel() {
        if window == nil {
            let host = NSHostingController(rootView: ControlPanelView(api: AppDelegate.api))
            let w = NSWindow(contentViewController: host)
            w.title = "QQ Agent 控制面板"
            w.styleMask = [.titled, .closable, .miniaturizable, .resizable]
            w.minSize = NSSize(width: 420, height: 600)
            w.setContentSize(NSSize(width: 460, height: 680))
            w.isReleasedWhenClosed = false
            w.center()
            window = w
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    private func sendFirstLaunchNotificationIfNeeded() {
        let key = "didShowFirstLaunchNotification"
        guard !UserDefaults.standard.bool(forKey: key) else { return }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            UserDefaults.standard.set(true, forKey: key)
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = "QQ Agent"
            content.body = "已在菜单栏运行，点右上角「QQ」打开控制面板"
            center.add(UNNotificationRequest(identifier: "firstLaunch", content: content, trigger: nil))
        }
    }
}

@main
struct QQAgentControllerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @ObservedObject private var api = AppDelegate.api

    var body: some Scene {
        MenuBarExtra {
            ControlPanelView(api: api)
        } label: {
            HStack(spacing: 2) {
                Image(systemName: api.globalActive
                      ? "antenna.radiowaves.left.and.right"
                      : "antenna.radiowaves.left.and.right.slash")
                Text("QQ")
            }
        }
        .menuBarExtraStyle(.window)
    }
}
