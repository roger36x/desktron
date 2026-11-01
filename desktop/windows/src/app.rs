//! 原生壳主体：窗口、主 WebView、前端桥、计时器、退出流程。
//!
//! 线程模型：所有 WebView 只在主线程访问。后端 HTTP、对话框与 Supervisor 在后台线程运行，
//! 结果经 `EventLoopProxy<UserEvent>` 回到主线程；异步回调以 token 登记在 `pending` 中。

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tao::dpi::{LogicalPosition, LogicalSize};
use tao::event::{Event, StartCause, WindowEvent};
use tao::event_loop::{ControlFlow, EventLoop, EventLoopBuilder, EventLoopProxy};
use tao::window::{Icon, Theme, Window, WindowBuilder};
use wry::{Rect, WebContext, WebView, WebViewBuilder, WebViewBuilderExtWindows, WebViewExtWindows};

use crate::backdrop;
use crate::chrome::Chrome;
use crate::dialogs;
use crate::http;
use crate::paths;
use crate::supervisor::{self, Phase, SupervisorEvent, SupervisorHandle, PORT};
use crate::views::BrowserView;
use crate::winutil;

pub const URL: &str = "http://127.0.0.1:7765/";
/// 所有 WebView 共用同一用户数据目录，浏览器参数必须一致。
/// Windows 公网浏览器沿用系统代理，不能照搬 macOS 的显式无代理配置；
/// Chromium 默认绕过 loopback，本地工作台仍直接访问 127.0.0.1。
/// 这是平台策略差异，不能为本机服务把所有公网页面强制直连。
pub const BROWSER_ARGS: &str = "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection";

const SHELL_SCRIPT: &str = include_str!("../js/main-bridge.js");
const CHROME_SCRIPT: &str = include_str!("../js/window-chrome.js");

pub enum UserEvent {
    MainIpc { url: String, body: String },
    BrowserIpc { id: String, body: String },
    TagIpc { body: String },
    Supervisor(SupervisorEvent),
    Http { token: u64, value: Option<Value>, error: Option<String> },
    Script { token: u64, result: String },
    Cdp { token: u64, result: Result<Value, String> },
    Dialog { token: u64, result: dialogs::Answer },
    BrowserNav { id: String, phase: &'static str },
    BrowserOpen { id: String, url: String },
    MainContentLoading,
    ExternalScheme { id: String, url: String },
    Download { id: String, phase: &'static str },
    QuitChecked { active: Option<i64> },
    QuitReady,
}

pub enum Reply {
    Http(Option<Value>, Option<String>),
    Value(Result<Value, String>),
    Dialog(dialogs::Answer),
}

pub type Cont = Box<dyn FnOnce(&mut App, Reply)>;
type TimerFn = Box<dyn FnOnce(&mut App)>;

pub struct App {
    pub window: Window,
    pub web: WebView,
    chrome: Chrome,
    acrylic: bool,
    pub context: WebContext,
    pub browser_args: String,
    pub proxy: EventLoopProxy<UserEvent>,
    pub supervisor: Option<SupervisorHandle>,
    pub started: Instant,
    pub log: Option<std::fs::File>,
    next_token: u64,
    pending: HashMap<u64, Cont>,
    timers: Vec<(Instant, u64, TimerFn)>,
    quit_pending: bool,
    pub quit_approved: bool,
    pub light: bool,
    // 内置浏览器
    pub browsers: HashMap<String, BrowserView>,
    pub browser_tab_ids: HashSet<String>,
    pub browser_trimmed_at: Instant,
    pub tags: Option<WebView>,
    pub tag_items: Vec<Value>,
    pub tag_payload: Value,
    pub tag_hover: String,
    // App Control
    pub app_control_token: String,
    pub leases: HashMap<String, String>,
    pub agent_leases: HashMap<String, String>,
    pub epochs: HashMap<String, i64>,
    pub paused: HashSet<String>,
    pub last_touch: HashMap<String, Instant>,
    pub pause_reason: HashMap<String, String>,
    pub isolated: HashSet<String>,
    pub native_input_until: HashMap<String, Instant>,
    pub poll_interval: Duration,
    pub polling: bool,
    pub poll_index: usize,
    pub poll_timer: u64,
    pub contexts: HashMap<String, i64>,
    pub desk_window_id: String,
    pub desk_resource_id: String,
    pub desk_session_id: String,
    pub desk_generation: i64,
    pub capability_reported_at: Option<Instant>,
    pub notified: HashSet<String>,
}

pub fn run() {
    if winutil::single_instance().map(std::mem::forget).is_none() {
        dialogs::alert_blocking("Desktron 已在运行", "已有一个 Desktron 窗口在运行。请切换到该窗口。");
        return;
    }
    let browser_args = match crate::network::browser_args(BROWSER_ARGS) {
        Ok(args) => args,
        Err(error) => {
            dialogs::alert_blocking("Desktron 网络配置错误", &error);
            return;
        }
    };
    let event_loop: EventLoop<UserEvent> = EventLoopBuilder::with_user_event().build();
    let proxy = event_loop.create_proxy();
    use tao::platform::windows::WindowBuilderExtWindows;
    let icon = load_icon();
    let window = WindowBuilder::new()
        .with_title("Desktron")
        .with_decorations(false)
        .with_transparent(true)
        .with_undecorated_shadow(true)
        .with_inner_size(LogicalSize::new(1160.0, 860.0))
        .with_min_inner_size(LogicalSize::new(900.0, 600.0))
        .with_theme(Some(Theme::Dark))
        .with_window_icon(icon.clone())
        // Tao 的窗口图标只设置 ICON_SMALL；任务栏还需要 ICON_BIG。
        .with_taskbar_icon(icon)
        .build(&event_loop)
        .expect("无法创建窗口");
    let backdrop_result = backdrop::apply(&window, false);
    let acrylic = backdrop_result.is_ok();
    let backdrop_reset_error = if !acrylic { backdrop::disable(&window).err() } else { None };
    let data_dir = webview_data_dir();
    let mut context = WebContext::new(Some(data_dir));
    let size = window.inner_size().to_logical::<f64>(window.scale_factor());
    let main_proxy = proxy.clone();
    let nav_proxy = proxy.clone();
    let parent = {
        use tao::platform::windows::WindowExtWindows;
        window.hwnd()
    };
    let web = WebViewBuilder::new_with_web_context(&mut context)
        .with_bounds(full_rect(size.width, size.height))
        // 先用安全纯色创建；透明配置单独执行，失败不能让整个 WebView 构建退出。
        .with_background_color(backdrop::background(false, false))
        .with_devtools(true)
        .with_additional_browser_args(&browser_args)
        .with_initialization_script_for_main_only(backdrop::PENDING_SCRIPT, true)
        .with_initialization_script_for_main_only(SHELL_SCRIPT, true)
        .with_initialization_script_for_main_only(CHROME_SCRIPT, true)
        .with_ipc_handler(move |request| {
            let url = request.uri().to_string();
            let _ = main_proxy.send_event(UserEvent::MainIpc { url, body: request.into_body() });
        })
        .with_navigation_handler(move |url| main_navigation_allowed(&url, &nav_proxy))
        .with_new_window_req_handler(|url, _| {
            open_external_http(&url);
            wry::NewWindowResponse::Deny
        })
        // 主页面生成的诊断文件等下载：先让人选择保存位置(对应 macOS 的保存面板)。
        .with_download_started_handler(move |source, path| {
            let suggested = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| {
                source.rsplit('/').next().unwrap_or("").split(['?', '#']).next().unwrap_or("").to_string()
            });
            match dialogs::save_file_blocking(parent, &suggested) {
                Some(target) => {
                    *path = target;
                    true
                }
                None => false,
            }
        })
        .with_html(placeholder("正在启动 Desktron 引擎…"))
        .build_as_child(&window)
        .expect("无法创建 WebView2；请确认已安装 Microsoft Edge WebView2 Runtime");
    let chrome = Chrome::new(&window, &web).expect("无法初始化 Windows 主区标题栏");
    let root = paths::repo_root();
    let app_control_token = uuid::Uuid::new_v4().to_string();
    let log = open_startup_log();
    let mut app = App {
        window,
        web,
        chrome,
        acrylic,
        context,
        browser_args,
        proxy: proxy.clone(),
        supervisor: None,
        started: Instant::now(),
        log,
        next_token: 1,
        pending: HashMap::new(),
        timers: Vec::new(),
        quit_pending: false,
        quit_approved: false,
        light: false,
        browsers: HashMap::new(),
        browser_tab_ids: HashSet::new(),
        browser_trimmed_at: Instant::now(),
        tags: None,
        tag_items: Vec::new(),
        tag_payload: Value::Null,
        tag_hover: String::new(),
        app_control_token: app_control_token.clone(),
        leases: HashMap::new(),
        agent_leases: HashMap::new(),
        epochs: HashMap::new(),
        paused: HashSet::new(),
        last_touch: HashMap::new(),
        pause_reason: HashMap::new(),
        isolated: HashSet::new(),
        native_input_until: HashMap::new(),
        poll_interval: Duration::ZERO,
        polling: false,
        poll_index: 0,
        poll_timer: 0,
        contexts: HashMap::new(),
        desk_window_id: format!("desk-{}", uuid::Uuid::new_v4()),
        desk_resource_id: String::new(),
        desk_session_id: String::new(),
        desk_generation: 0,
        capability_reported_at: None,
        notified: HashSet::new(),
    };
    if let Err(error) = backdrop_result {
        app.startup_log(&error);
        app.startup_log("window_backdrop_mode=solid");
    } else {
        app.startup_log("window_backdrop_mode=acrylic_requested");
    }
    if let Some(error) = backdrop_reset_error { app.startup_log(&error); }
    app.sync_backdrop_background();
    app.attach_main_events();
    // 取活循环从一开始就运行：Agent 自开页的第一个动作就是开页本身。
    app.ensure_app_control_timer();
    let spec = supervisor::LaunchSpec {
        python: paths::python(&root),
        root,
        app_control_token,
        host_rev: paths::read_resource("release-rev"),
        host_build: paths::read_resource("release-build"),
    };
    let sup_proxy = proxy.clone();
    app.supervisor = Some(supervisor::start(
        spec,
        Box::new(move |event| {
            let _ = sup_proxy.send_event(UserEvent::Supervisor(event));
        }),
    ));

    event_loop.run(move |event, _target, control_flow| {
        match event {
            Event::NewEvents(StartCause::Init) => {}
            Event::UserEvent(user) => app.handle(user, control_flow),
            Event::WindowEvent { event: WindowEvent::Resized(_), .. }
            | Event::WindowEvent { event: WindowEvent::ScaleFactorChanged { .. }, .. } => app.layout_main(),
            Event::WindowEvent { event: WindowEvent::CloseRequested, .. } => app.request_quit(),
            Event::WindowEvent { event: WindowEvent::Focused(true), .. } => app.report_desktop_capability(false),
            _ => {}
        }
        app.fire_timers();
        if app.quit_approved {
            *control_flow = ControlFlow::Exit;
        } else {
            *control_flow = match app.next_timer() {
                Some(at) => ControlFlow::WaitUntil(at),
                None => ControlFlow::Wait,
            };
        }
    });
}

fn load_icon() -> Option<Icon> {
    let bytes = include_bytes!("../icons/desktron-64.rgba");
    Icon::from_rgba(bytes.to_vec(), 64, 64).ok()
}

fn webview_data_dir() -> PathBuf {
    std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(paths::share).join("Desktron").join("WebView2")
}

fn open_startup_log() -> Option<std::fs::File> {
    let _ = std::fs::create_dir_all(paths::logs_dir());
    std::fs::OpenOptions::new().create(true).append(true).open(paths::logs_dir().join("native-host.log")).ok()
}

pub fn full_rect(width: f64, height: f64) -> Rect {
    Rect { position: LogicalPosition::new(0.0, 0.0).into(), size: LogicalSize::new(width, height).into() }
}

pub fn placeholder(text: &str) -> String {
    format!(
        "<body style='background:#1a1a1a;color:#ddd;font:15px \"Segoe UI\",\"Microsoft YaHei UI\",sans-serif;\
display:flex;height:100vh;margin:0;align-items:center;justify-content:center;padding:40px;text-align:center'>{}</body>",
        html_escape(text)
    )
}

pub fn html_escape(text: &str) -> String {
    text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

/// 主 WebView 只加载应用自己那一页；外链交给系统浏览器(与 macOS 壳的兜底规则一致)。
fn main_navigation_allowed(url: &str, _proxy: &EventLoopProxy<UserEvent>) -> bool {
    if url.starts_with("about:") || url.starts_with("data:") || url.starts_with("blob:") {
        return true;
    }
    if let Some(rest) = url.strip_prefix("http://127.0.0.1:7765") {
        let path = rest.split(['?', '#']).next().unwrap_or("");
        if path.is_empty() || path == "/" || path == "/index.html" {
            return true;
        }
        return false;
    }
    open_external_http(url);
    false
}

pub fn open_external_http(url: &str) {
    let lower = url.to_ascii_lowercase();
    if (lower.starts_with("http://") || lower.starts_with("https://"))
        && !lower.starts_with("http://127.0.0.1")
        && !lower.starts_with("http://localhost")
    {
        winutil::shell_open(url);
    }
}

pub fn js_string(value: &str) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "\"\"".into())
}

impl App {
    // ------------------------------------------------------------------ 基础设施

    pub fn token(&mut self) -> u64 {
        self.next_token += 1;
        self.next_token
    }

    pub fn wait(&mut self, done: Cont) -> u64 {
        let token = self.token();
        self.pending.insert(token, done);
        token
    }

    fn resolve(&mut self, token: u64, reply: Reply) {
        if let Some(done) = self.pending.remove(&token) {
            done(self, reply);
        }
    }

    pub fn after(&mut self, delay: Duration, action: impl FnOnce(&mut App) + 'static) -> u64 {
        let id = self.token();
        self.timers.push((Instant::now() + delay, id, Box::new(action)));
        id
    }

    pub fn cancel_timer(&mut self, id: u64) {
        self.timers.retain(|(_, timer, _)| *timer != id);
    }

    fn next_timer(&self) -> Option<Instant> {
        self.timers.iter().map(|(at, _, _)| *at).min()
    }

    fn fire_timers(&mut self) {
        loop {
            let now = Instant::now();
            let Some(index) = self.timers.iter().position(|(at, _, _)| *at <= now) else { break };
            let (_, _, action) = self.timers.remove(index);
            action(self);
        }
    }

    pub fn startup_log(&mut self, phase: &str) {
        use std::io::Write;
        let elapsed = self.started.elapsed().as_millis();
        if let Some(log) = self.log.as_mut() {
            let _ = writeln!(log, "[startup] native {phase} {elapsed}ms");
        }
    }

    /// 访问本机后端(带宿主令牌)；完成后在主线程调用 done。
    pub fn backend(
        &mut self,
        path: &str,
        method: &str,
        body: Option<Value>,
        done: impl FnOnce(&mut App, Option<Value>, Option<String>) + 'static,
    ) {
        let token = self.wait(Box::new(move |app, reply| {
            if let Reply::Http(value, error) = reply {
                done(app, value, error);
            }
        }));
        let proxy = self.proxy.clone();
        let host = self.app_control_token.clone();
        let path = path.to_string();
        let method = method.to_string();
        thread::spawn(move || {
            let (value, error) = http::json(
                PORT,
                &method,
                &path,
                &[("X-Desk-App-Control-Host", host.as_str())],
                body.as_ref(),
                Duration::from_secs(30),
            );
            let _ = proxy.send_event(UserEvent::Http { token, value, error });
        });
    }

    /// 在主页面执行脚本(页面世界)，忽略结果。
    pub fn eval_main(&self, js: &str) {
        let _ = self.web.evaluate_script(js);
    }

    /// 在指定 WebView 的页面世界执行脚本并取 JSON 结果。
    pub fn eval_with(
        &mut self,
        target: Option<&str>,
        js: &str,
        done: impl FnOnce(&mut App, Result<Value, String>) + 'static,
    ) {
        let token = self.wait(Box::new(move |app, reply| {
            if let Reply::Value(result) = reply {
                done(app, result);
            }
        }));
        let proxy = self.proxy.clone();
        let callback = move |result: String| {
            let _ = proxy.send_event(UserEvent::Script { token, result });
        };
        let outcome = match target {
            None => self.web.evaluate_script_with_callback(js, callback),
            Some(id) => match self.browsers.get(id) {
                Some(view) => view.webview.evaluate_script_with_callback(js, callback),
                None => {
                    self.resolve(token, Reply::Value(Err("browser_tab_unavailable".into())));
                    return;
                }
            },
        };
        if let Err(error) = outcome {
            self.resolve(token, Reply::Value(Err(error.to_string())));
        }
    }

    // ------------------------------------------------------------------ 事件分发

    fn handle(&mut self, event: UserEvent, _control_flow: &mut ControlFlow) {
        match event {
            UserEvent::MainIpc { url, body } => self.on_main_message(&url, &body),
            UserEvent::BrowserIpc { id, body } => self.on_browser_message(&id, &body),
            UserEvent::TagIpc { body } => self.on_tag_message(&body),
            UserEvent::Supervisor(event) => self.on_supervisor(event),
            UserEvent::Http { token, value, error } => self.resolve(token, Reply::Http(value, error)),
            UserEvent::Script { token, result } => {
                let value = serde_json::from_str::<Value>(&result).map_err(|e| e.to_string());
                self.resolve(token, Reply::Value(value));
            }
            UserEvent::Cdp { token, result } => self.resolve(token, Reply::Value(result)),
            UserEvent::Dialog { token, result } => self.resolve(token, Reply::Dialog(result)),
            UserEvent::BrowserNav { id, phase } => self.on_browser_nav(&id, phase),
            UserEvent::BrowserOpen { id, url } => self.on_browser_open(&id, &url),
            UserEvent::MainContentLoading => {
                self.park_native_overlays();
                self.sync_backdrop_marker();
            }
            UserEvent::ExternalScheme { id, url } => self.handoff_external_scheme(&id, &url),
            UserEvent::Download { id, phase } => self.on_download(&id, phase),
            UserEvent::QuitChecked { active } => self.confirm_quit(active),
            UserEvent::QuitReady => {
                self.quit_pending = false;
                self.quit_approved = true;
            }
        }
        // Browser/tag原生视图可能刚被置顶；边缘输入层必须始终位于它们之前。
        if let Err(error) = self.chrome.raise_resize() { self.startup_log(&error); }
    }

    fn on_supervisor(&mut self, event: SupervisorEvent) {
        match event {
            SupervisorEvent::Healthy(probe) => {
                let boot = probe.boot_id.unwrap_or_default();
                self.startup_log(&format!("backend ready boot={boot}"));
                self.report_desktop_capability(true);
                let _ = self.web.load_url(URL);
            }
            SupervisorEvent::Phase(Phase::ForeignPort(pid)) => {
                let detail = pid.map(|p| p.to_string()).unwrap_or_else(|| "未知".into());
                let _ = self.web.load_html(&placeholder(&format!(
                    "端口 {PORT} 被非当前后端占用(PID {detail})。Desktron 未接管或终止该进程。"
                )));
            }
            SupervisorEvent::Phase(Phase::Failed(reason)) => {
                let log = paths::logs_dir().join("desktron-web.log");
                let _ =
                    self.web.load_html(&placeholder(&format!("引擎连续启动失败：{reason}。日志：{}", log.display())));
            }
            SupervisorEvent::Phase(_) => {}
        }
    }

    /// 主页面消息：只接受 127.0.0.1:7765 主框架发出的消息(wry 的 IPC 只来自主框架)。
    fn on_main_message(&mut self, url: &str, body: &str) {
        if !url.starts_with("http://127.0.0.1:7765/") {
            return;
        }
        let Ok(message) = serde_json::from_str::<Value>(body) else { return };
        let name = message.get("name").and_then(Value::as_str).unwrap_or("");
        let b = message.get("body").cloned().unwrap_or(Value::Null);
        let s = |key: &str| b.get(key).and_then(Value::as_str).unwrap_or("").to_string();
        match name {
            "startup" => {
                let phase = s("phase");
                let ms = b.get("ms").and_then(Value::as_f64).unwrap_or(-1.0);
                let line = format!("frontend {} web={:.0}ms {}", phase, ms, s("detail"));
                self.startup_log(&line);
                if phase == "interactive" {
                    self.replay_app_control_state();
                }
            }
            "windowChrome" => {
                if let Some(left) = b.get("left").and_then(Value::as_f64) {
                    if let Err(error) = self.chrome.set_left(left) {
                        self.startup_log(&error);
                    }
                }
            }
            // 双顶栏：拖窗与系统按钮由上层原生标题区处理，下层页面控件不拖窗。
            "windowDrag" | "windowZoom" => {}
            "deskControlUser" => {
                if b.get("controlPlane").and_then(Value::as_bool) == Some(true) || self.desk_resource_id.is_empty() {
                    return;
                }
                self.desk_generation += 1;
                let id = self.desk_resource_id.clone();
                self.pause_app_control(&id, "user_interaction");
            }
            "deskControl" => match s("action").as_str() {
                "delegate" => self.delegate_desk_control(&s("sessionId")),
                "takeover" => self.revoke_desk_control("user_takeover"),
                "resourceChanged" => self.revoke_desk_control("resource_changed"),
                _ => {}
            },
            "desktopNotification" => self.deliver_notification(&s("eventId"), &s("kind")),
            "approvalControl" => self.approval_control(&b),
            "browser" => self.on_browser_command(&b),
            _ => {}
        }
    }

    fn approval_control(&mut self, b: &Value) {
        let s = |key: &str| b.get(key).and_then(Value::as_str).unwrap_or("").to_string();
        let request_id = s("requestId");
        if request_id.is_empty() {
            return;
        }
        let (path, method, body): (&str, &str, Option<Value>) = match s("action").as_str() {
            "list" => ("/workbench/app-control/approvals?include_resolved=1&limit=200", "GET", None),
            "grantsList" => ("/workbench/app-control/grants?app_id=browser", "GET", None),
            "grantRevoke" => (
                "/workbench/app-control/grants",
                "DELETE",
                Some(json!({"app_id": "browser", "origin": s("origin"), "scope_key": s("scopeKey")})),
            ),
            "decide" => {
                let mut body = json!({"operation_id": s("operationId"),
                    "approved": b.get("approved").and_then(Value::as_bool).unwrap_or(false)});
                if let Some(anchor) = b.get("anchor").filter(|a| a.is_object()) {
                    body["anchor"] = anchor.clone();
                }
                ("/workbench/app-control/approvals/decide", "POST", Some(body))
            }
            "clear" => (
                "/workbench/app-control/approvals/clear",
                "POST",
                Some(json!({"before": b.get("before").cloned().unwrap_or(json!(0))})),
            ),
            _ => {
                self.send_approval_result(&request_id, None, Some("unsupported_approval_action".into()));
                return;
            }
        };
        self.backend(path, method, body, move |app, value, error| {
            app.send_approval_result(&request_id, value, error);
        });
    }

    fn send_approval_result(&self, request_id: &str, result: Option<Value>, error: Option<String>) {
        let value = json!({"requestId": request_id, "result": result.unwrap_or(Value::Null),
                           "error": error.unwrap_or_default()});
        self.eval_main(&format!("window.__approvalNativeResult && window.__approvalNativeResult({value})"));
    }

    fn deliver_notification(&mut self, event_id: &str, kind: &str) {
        let valid_id = !event_id.is_empty()
            && event_id.len() <= 180
            && event_id.chars().all(|c| c.is_ascii_alphanumeric() || c == ':' || c == '_' || c == '-');
        let message = match kind {
            "update_succeeded" => ("Desktron 更新完成", "新版本已安装并正常启动。"),
            "update_failed" => ("Desktron 更新未完成", "请在设置中查看更新状态和日志。"),
            _ => return,
        };
        if !valid_id || !self.notified.insert(event_id.to_string()) {
            return;
        }
        let (title, text) = (message.0.to_string(), message.1.to_string());
        thread::spawn(move || {
            use tauri_winrt_notification::Toast;
            let _ = Toast::new(Toast::POWERSHELL_APP_ID).title(&title).text1(&text).show();
        });
    }

    // ------------------------------------------------------------------ 布局与外观

    pub fn content_size(&self) -> (f64, f64) {
        let size = self.window.inner_size().to_logical::<f64>(self.window.scale_factor());
        (size.width, size.height)
    }

    fn layout_main(&mut self) {
        let (w, h) = self.content_size();
        if w <= 0.0 || h <= 0.0 { return; }
        if let Err(error) = self.web.set_bounds(full_rect(w, h)) {
            self.startup_log(&format!("window_chrome_web_bounds_failed: {error}"));
            return; // 子窗口尺寸更新失败时不能继续应用新父窗口坐标的裁剪区。
        }
        if let Err(error) = self.chrome.layout() {
            self.startup_log(&error);
        }
    }

    pub fn set_appearance(&mut self, light: bool) {
        self.light = light;
        self.chrome.set_light(light);
        self.window.set_theme(Some(if light { Theme::Light } else { Theme::Dark }));
        self.acrylic = match backdrop::apply(&self.window, light) {
            Ok(()) => true,
            Err(error) => {
                self.startup_log(&error);
                if let Err(reset) = backdrop::disable(&self.window) { self.startup_log(&reset); }
                self.startup_log("window_backdrop_mode=solid");
                false
            }
        };
        self.sync_backdrop_background();
        self.sync_backdrop_marker();
    }

    fn sync_backdrop_background(&mut self) {
        if let Err(error) = self.web.set_background_color(backdrop::background(self.acrylic, self.light)) {
            self.startup_log(&format!("window_backdrop_webview_failed: {error}"));
            self.acrylic = false;
            if let Err(reset) = backdrop::disable(&self.window) { self.startup_log(&reset); }
            self.startup_log("window_backdrop_mode=solid");
            if let Err(fallback) = self.web.set_background_color(backdrop::background(false, self.light)) {
                self.startup_log(&format!("window_backdrop_solid_webview_failed: {fallback}"));
            }
        }
        self.chrome.set_acrylic(self.acrylic);
    }

    fn sync_backdrop_marker(&mut self) {
        if let Err(error) = self.web.evaluate_script(backdrop::script(self.acrylic)) {
            self.startup_log(&format!("window_backdrop_marker_failed: {error}"));
        }
    }

    fn attach_main_events(&mut self) {
        use webview2_com::ContentLoadingEventHandler;
        let proxy = self.proxy.clone();
        let core = self.web.webview();
        let handler = ContentLoadingEventHandler::create(Box::new(move |_, _| {
            let _ = proxy.send_event(UserEvent::MainContentLoading);
            Ok(())
        }));
        let mut token = Default::default();
        unsafe {
            let _ = core.add_ContentLoading(&handler, &mut token);
        }
    }

    pub fn parent_hwnd(&self) -> isize {
        use tao::platform::windows::WindowExtWindows;
        self.window.hwnd()
    }

    // ------------------------------------------------------------------ 退出

    fn request_quit(&mut self) {
        if self.quit_approved || self.quit_pending {
            return;
        }
        self.quit_pending = true;
        let proxy = self.proxy.clone();
        thread::spawn(move || {
            let active = http::request(PORT, "GET", "/workbench/runtime", &[], None, Duration::from_secs(2))
                .ok()
                .filter(|r| r.status == 200)
                .and_then(|r| serde_json::from_slice::<Value>(&r.body).ok())
                .and_then(|v| v.get("active_work").and_then(Value::as_i64));
            let _ = proxy.send_event(UserEvent::QuitChecked { active });
        });
    }

    fn confirm_quit(&mut self, active: Option<i64>) {
        let needs_confirm = active.map(|n| n > 0).unwrap_or(true);
        if !needs_confirm {
            self.finish_quit();
            return;
        }
        let detail = match active {
            None => "暂时无法确认执行状态。退出会中断正在运行的回合；未确认的结果将在下次打开时核验，不会自动重跑。"
                .to_string(),
            Some(n) => format!("仍有 {n} 项工作正在执行。退出会请求中断；队列保留，重新打开后需手动继续。"),
        };
        let parent = self.parent_hwnd();
        let token = self.wait(Box::new(|app, reply| {
            if let Reply::Dialog(dialogs::Answer::Confirmed(true)) = reply {
                app.finish_quit();
            } else {
                app.quit_pending = false;
            }
        }));
        dialogs::confirm(
            self.proxy.clone(),
            token,
            parent,
            "确认退出 Desktron？".into(),
            detail,
            "中断并退出".into(),
            "继续等待".into(),
        );
    }

    fn finish_quit(&mut self) {
        let Some(supervisor) = self.supervisor.take() else {
            self.quit_approved = true;
            return;
        };
        let proxy = self.proxy.clone();
        thread::spawn(move || {
            supervisor.shutdown_blocking();
            let _ = proxy.send_event(UserEvent::QuitReady);
        });
    }
}
