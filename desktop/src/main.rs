// desktron 桌面壳:启动时在后台拉起本地 Desk 服务,
// 轮询就绪后把窗口导航到本地 URL;关窗时收掉子进程。用户只见一个独立窗口。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

const PORT: u16 = 7766;

struct Server(Mutex<Option<Child>>);

fn repo_root() -> std::path::PathBuf {
    // 优先 DESKTRON_HOME;否则按 app 同级 Resources 约定;开发期回退到环境变量
    if let Ok(p) = std::env::var("DESKTRON_HOME") {
        return std::path::PathBuf::from(p);
    }
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| std::path::PathBuf::from("."))
}

fn spawn_server(root: &std::path::Path) -> std::io::Result<Child> {
    let cli = root.join("desktron/bin/desktron");
    Command::new("bash")
        .arg(cli)
        .arg("web")
        .arg("--port")
        .arg(PORT.to_string())
        .current_dir(root)
        .spawn()
}

fn wait_ready(url: &str) -> bool {
    for _ in 0..60 {
        if std::net::TcpStream::connect(("127.0.0.1", PORT)).is_ok() {
            // 端口通后再给前端一点加载时间
            std::thread::sleep(Duration::from_millis(400));
            let _ = url;
            return true;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Server(Mutex::new(None)))
        .setup(|app| {
            let root = repo_root();
            let child = spawn_server(&root).ok();
            app.state::<Server>().0.lock().unwrap().replace(child.expect("desktron web 启动失败"));

            let handle = app.handle().clone();
            let url = format!("http://127.0.0.1:{PORT}/");
            std::thread::spawn(move || {
                let loaded = wait_ready(&url);
                let target = if loaded {
                    WebviewUrl::External(url.parse().unwrap())
                } else {
                    WebviewUrl::App("error.html".into())
                };
                let _ = WebviewWindowBuilder::new(&handle, "main", target)
                    .title("desktron")
                    .inner_size(1280.0, 860.0)
                    .min_inner_size(900.0, 600.0)
                    .build();
                // 关闭启动占位窗
                if let Some(w) = handle.get_webview_window("boot") {
                    let _ = w.close();
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if window.label() == "main" {
                    if let Some(mut child) = window.state::<Server>().0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                    window.app_handle().exit(0);
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("启动 desktron 桌面壳失败");
}
