//! 窗口导航守卫
//!
//! 渲染层的点击拦截只能盖住 `<a>` 与 `window.open`；脚本改 `location`、
//! 表单提交、服务端 30x 都不经过它。一旦远程页面进入 webview，就会继承
//! 该窗口的 IPC 能力，所以这里在 Rust 侧兜底：
//! - 只允许应用自身页面（tauri 协议 / 本地开发服务器）继续导航；
//! - 其余 http(s)/mailto 一律取消并交给系统浏览器；
//! - 新窗口请求全部拒绝（应用内的独立窗口由 Rust 自行创建）。

use tauri::webview::{NewWindowFeatures, NewWindowResponse};
use tauri::{Runtime, Url};

/// 应用自身页面：生产为 tauri 协议，开发为本地 Vite 服务
pub fn is_app_url(url: &Url) -> bool {
    match url.scheme() {
        "tauri" | "ipc" | "about" => true,
        "http" | "https" => match url.host_str() {
            Some("tauri.localhost") => true,
            Some("localhost") | Some("127.0.0.1") | Some("::1") => tauri::is_dev(),
            _ => false,
        },
        _ => false,
    }
}

fn open_external(url: &Url) {
    if !matches!(url.scheme(), "http" | "https" | "mailto" | "tel") {
        return;
    }
    let target = url.to_string();
    // 在导航回调里同步启动浏览器会阻塞 webview 线程，交给独立线程
    std::thread::spawn(move || {
        if let Err(e) = tauri_plugin_opener::open_url(&target, None::<&str>) {
            eprintln!("[navigation] 外链打开失败 {target}: {e}");
        }
    });
}

/// 主窗口/任务窗口的导航白名单
pub fn allow_navigation(url: &Url) -> bool {
    if is_app_url(url) {
        return true;
    }
    open_external(url);
    false
}

/// 页面脚本发起的 `window.open`：不开新窗口，外链仍交给系统浏览器
pub fn new_window<R: Runtime>(url: Url, _features: NewWindowFeatures) -> NewWindowResponse<R> {
    if is_app_url(&url) {
        return NewWindowResponse::Allow;
    }
    open_external(&url);
    NewWindowResponse::Deny
}

/// 主窗口：配置里声明的窗口挂不上导航守卫，统一在 Rust 侧创建
pub fn create_main_window(app: &tauri::App) -> tauri::Result<tauri::WebviewWindow> {
    tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("index.html".into()))
        .title("任我行")
        .inner_size(1280.0, 800.0)
        .min_inner_size(960.0, 640.0)
        .center()
        .on_navigation(allow_navigation)
        .on_new_window(new_window)
        .build()
}

#[cfg(test)]
mod tests {
    use super::is_app_url;

    fn url(raw: &str) -> tauri::Url {
        raw.parse().expect("测试 URL 应合法")
    }

    #[test]
    fn accepts_app_schemes() {
        assert!(is_app_url(&url("tauri://localhost/index.html")));
        assert!(is_app_url(&url("http://tauri.localhost/index.html")));
    }

    #[test]
    fn rejects_remote_sites() {
        assert!(!is_app_url(&url("https://example.com/")));
        assert!(!is_app_url(&url("http://evil.localhost.evil.com/")));
        assert!(!is_app_url(&url("file:///C:/Windows/System32/")));
    }
}