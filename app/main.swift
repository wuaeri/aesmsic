import Cocoa
import WebKit

/* aesmsic —— MusicLab 的桌面外壳
 *
 * 它自己不干音乐相关的活。所有分析、分轨、索引都在仓库根目录里，
 * 这个 app 干三件事：
 *   1. 开一个窗口，把 WKWebView 指向本机的 8900
 *   2. 服务没在跑就替你拉起来（server.py + pipeline.py）
 *   3. 关掉的时候，只停它自己拉起来的那些进程
 *
 * ★ 第 3 条是关键：日常调试时经常手动起了 server 再开 app，
 *   那种进程不归 app 管。所以 spawned 里只记自己 spawn 的 PID，
 *   退出时只 terminate 这些。
 */

// lab 目录 = 仓库根。三档优先级：
//   1. 环境变量（调试 / 同时跑多份副本）
//   2. Info.plist 里的 AESMSICLabPath —— build.sh 编译时写进去的，跟着仓库走
//   3. 老默认值，兜底
let LAB = ProcessInfo.processInfo.environment["AESMSIC_LAB"]
    ?? (Bundle.main.object(forInfoDictionaryKey: "AESMSICLabPath") as? String)
    ?? NSString(string: "~/Projects/MusicLab").expandingTildeInPath
let PORT = 8900
let BASE = "http://127.0.0.1:\(PORT)"

var spawned: [Process] = []

func serverAlive() -> Bool {
    guard let u = URL(string: "\(BASE)/api/library") else { return false }
    var req = URLRequest(url: u)
    req.timeoutInterval = 1.2
    req.cachePolicy = .reloadIgnoringLocalCacheData
    var ok = false
    let sem = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: req) { _, resp, _ in
        if let h = resp as? HTTPURLResponse, h.statusCode == 200 { ok = true }
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + 2)
    return ok
}

func spawn(_ script: String) -> Process? {
    let py = "\(LAB)/.venv/bin/python"
    guard FileManager.default.isExecutableFile(atPath: py),
          FileManager.default.fileExists(atPath: "\(LAB)/\(script)") else { return nil }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: py)
    p.arguments = ["\(LAB)/\(script)"]
    p.currentDirectoryURL = URL(fileURLWithPath: LAB)
    let log = "/tmp/aesmsic_\(script.replacingOccurrences(of: ".py", with: "")).log"
    FileManager.default.createFile(atPath: log, contents: nil)
    if let fh = FileHandle(forWritingAtPath: log) {
        p.standardOutput = fh
        p.standardError = fh
    }
    do { try p.run(); return p } catch { return nil }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var win: NSWindow!
    var web: WKWebView!
    var retried = false

    func applicationDidFinishLaunching(_ n: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMenu()

        win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
                       styleMask: [.titled, .closable, .miniaturizable, .resizable],
                       backing: .buffered, defer: false)
        win.title = "aesmsic"
        win.appearance = NSAppearance(named: .darkAqua)
        win.minSize = NSSize(width: 900, height: 560)
        win.center()
        win.setFrameAutosaveName("AesmsicMain")
        win.titlebarAppearsTransparent = true
        win.backgroundColor = NSColor(calibratedRed: 0.043, green: 0.043, blue: 0.051, alpha: 1)

        let cfg = WKWebViewConfiguration()
        // 播放器要能自己出声，不能等一次点击
        cfg.mediaTypesRequiringUserActionForPlayback = []
        web = WKWebView(frame: win.contentView!.bounds, configuration: cfg)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.underPageBackgroundColor = NSColor(calibratedRed: 0.043, green: 0.043, blue: 0.051, alpha: 1)
        web.setValue(false, forKey: "drawsBackground")
        win.contentView = web

        win.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        boot()
    }

    func boot() {
        DispatchQueue.global().async {
            if !serverAlive() {
                if let s = spawn("server.py") { spawned.append(s) }
                if let p = spawn("pipeline.py") { spawned.append(p) }
                for _ in 0..<40 {                       // 最多等 20 秒
                    Thread.sleep(forTimeInterval: 0.5)
                    if serverAlive() { break }
                }
            }
            DispatchQueue.main.async {
                self.load()
            }
        }
    }

    func load() {
        web.load(URLRequest(url: URL(string: "\(BASE)/player")!))
    }

    // 服务刚起来时第一次加载可能抢跑失败 —— 自动重试一次，不让他看见白屏
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        guard !retried else {
            let msg = "<body style=\"background:#0b0b0d;color:#8a8a90;font:14px -apple-system;padding:60px\">"
                + "<h2 style=\"color:#ffa42b;font-weight:500\">连不上本地服务</h2>"
                + "<p>8900 端口没有响应。</p><p style=\"color:#555\">"
                + "手动检查：<code style=\"color:#aaa\">\(LAB)</code> 是否还在，"
                + "以及 <code style=\"color:#aaa\">.venv</code> 有没有被删。</p></body>"
            webView.loadHTMLString(msg, baseURL: nil)
            return
        }
        retried = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in self?.load() }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        retried = false
    }

    // 关窗口不等于退出 —— 点 Dock 还能回来（aemy 栽过这个跟头）
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { win.makeKeyAndOrderFront(nil) }
        return true
    }

    func applicationWillTerminate(_ n: Notification) {
        for p in spawned where p.isRunning { p.terminate() }
    }

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 aesmsic", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 aesmsic", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出 aesmsic", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "显示")
        viewMenu.addItem(withTitle: "重新载入", action: #selector(reload), keyEquivalent: "r")
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        main.addItem(editItem)

        let winItem = NSMenuItem()
        let winMenu = NSMenu(title: "窗口")
        winMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "缩放", action: #selector(NSWindow.zoom(_:)), keyEquivalent: "")
        winItem.submenu = winMenu
        main.addItem(winItem)

        NSApp.mainMenu = main
    }

    @objc func reload() { load() }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
