//! 内置浏览器：原生 WebView2 面板(绕过 iframe 的 X-Frame-Options/CSP)、悬浮标签条、
//! 导航策略、外部协议与下载。行为对应 macOS 壳的 openBrowser / renderTags / decidePolicyFor。

use std::cell::RefCell;
use std::collections::HashSet;
use std::rc::Rc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tao::dpi::{LogicalPosition, LogicalSize};
use webview2_com::Microsoft::Web::WebView2::Win32::{ICoreWebView2, ICoreWebView2Settings2};
use webview2_com::{
    ContentLoadingEventHandler, HistoryChangedEventHandler, NavigationCompletedEventHandler,
    NavigationStartingEventHandler, SourceChangedEventHandler,
};
use windows::core::{Interface, PWSTR};
use windows::Win32::Foundation::{HWND, RECT};
use windows::Win32::Graphics::Gdi::{CombineRgn, CreateEllipticRgn, CreateRectRgn, DeleteObject, SetWindowRgn, RGN_OR};
use windows::Win32::UI::WindowsAndMessaging::{SetWindowPos, HWND_TOP, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE};
use wry::{
    BackgroundThrottlingPolicy, NewWindowResponse, Rect, WebViewBuilder, WebViewBuilderExtWindows, WebViewExtWindows,
};

use crate::app::{js_string, App, Reply, UserEvent};
use crate::dialogs;
use crate::winutil;

const GUARD_SCRIPT: &str = include_str!("../js/browser-guard.js");
const NETWORK_TRACKER: &str = include_str!("../../shared/js/network-tracker.js");
const LARK_SELECT: &str = include_str!("../../shared/js/lark-select-chat.js");
const LARK_REFERENCE: &str = include_str!("../../shared/js/lark-message-reference.js");
const LARK_CONTEXT: &str = include_str!("../../shared/js/lark-reference-context.js");
const TAGS_PAGE: &str = include_str!("../js/tags.html");

const WEB_SCHEMES: &[&str] = &["http", "https", "about", "blob", "data", "file", "javascript", "ws", "wss"];
const TRUSTED_APP_SCHEMES: &[&str] = &["lark", "feishu", "larksuite", "mailto", "tel"];
const FEISHU_APPLINK_HOSTS: &[&str] = &["applink.feishu.cn", "applink.larksuite.com"];

/// 导航回调需要同步读取的单页策略。
#[derive(Default)]
pub struct ViewPolicy {
    pub allowed_hosts: HashSet<String>,
}

pub struct BrowserView {
    pub webview: wry::WebView,
    pub mode: String,
    pub hidden: bool,
    pub last_used: Instant,
    pub policy: Rc<RefCell<ViewPolicy>>,
}

fn is_lark_select(mode: &str) -> bool {
    mode == "lark-select-chat" || mode == "lark-select-chat-no-collapse"
}
fn is_lark_automation(mode: &str) -> bool {
    is_lark_select(mode) || mode == "lark-search-chat" || mode == "lark-search-select-chat"
}
fn is_lark_reference(mode: &str) -> bool {
    mode == "lark-message-reference"
}
fn is_chrome_ua(mode: &str) -> bool {
    is_lark_select(mode) || is_lark_reference(mode) || mode == "chrome-ua"
}

/// 只在主框架、且文档解析完成后执行(对应 WKUserScript atDocumentEnd + forMainFrameOnly)。
fn at_document_end(script: &str) -> String {
    format!(
        "(() => {{ if (window.top !== window) return; const run = () => {{\n{script}\n}};\n\
         if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, {{once: true}}); else run(); }})();"
    )
}

pub fn lark_select_script(chat: &str, tag: &str, click: bool) -> String {
    // 占位符位于 JS 字符串字面量内：替换为 JSON 转义后去掉外层引号的内容。
    let inner = |value: &str| {
        let quoted = js_string(value);
        quoted[1..quoted.len() - 1].to_string()
    };
    LARK_SELECT
        .replace("__DESKTRON_TARGET_CHAT__", &inner(chat))
        .replace("__DESKTRON_TARGET_TAG__", &inner(tag))
        .replace("__DESKTRON_CLICK_RESULT__", if click { "true" } else { "false" })
}

pub fn feishu_client_url(url: &str) -> Option<String> {
    let lower = url.to_ascii_lowercase();
    let rest = lower.strip_prefix("https://")?;
    let host = rest.split(['/', '?', '#']).next().unwrap_or("");
    if FEISHU_APPLINK_HOSTS.contains(&host) {
        Some(format!("lark://{}", &url["https://".len()..]))
    } else {
        None
    }
}

fn scheme_of(url: &str) -> String {
    url.split(':').next().unwrap_or("").to_ascii_lowercase()
}

/// 非网页协议或飞书 applink：返回需要交给系统处理的地址。
pub fn external_target(url: &str) -> Option<String> {
    let target = feishu_client_url(url).unwrap_or_else(|| url.to_string());
    let scheme = scheme_of(&target);
    if scheme.is_empty() || WEB_SCHEMES.contains(&scheme.as_str()) {
        None
    } else {
        Some(target)
    }
}

fn host_of(url: &str) -> Option<String> {
    let rest = url.split("://").nth(1)?;
    let authority = rest.split(['/', '?', '#']).next()?;
    let host = authority.rsplit('@').next()?.split(':').next()?;
    Some(host.trim_matches('.').to_ascii_lowercase())
}

fn rect_from(value: &Value) -> Option<(f64, f64, f64, f64)> {
    let n = |key: &str| value.get(key).and_then(Value::as_f64).unwrap_or(0.0);
    if !value.is_object() {
        return None;
    }
    Some((n("x"), n("y"), n("w"), n("h")))
}

fn to_rect(x: f64, y: f64, w: f64, h: f64) -> Rect {
    Rect { position: LogicalPosition::new(x, y).into(), size: LogicalSize::new(w.max(1.0), h.max(1.0)).into() }
}

pub fn raise(hwnd: HWND) {
    unsafe {
        let _ = SetWindowPos(hwnd, Some(HWND_TOP), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    }
}

fn webview_title(core: &ICoreWebView2) -> String {
    unsafe {
        let mut title = PWSTR::null();
        if core.DocumentTitle(&mut title).is_ok() && !title.is_null() {
            let value = title.to_string().unwrap_or_default();
            windows::Win32::System::Com::CoTaskMemFree(Some(title.0 as *const _));
            return value;
        }
    }
    String::new()
}

impl App {
    pub fn browser_view_log(&self, id: &str, what: &str, extra: &str) {
        use std::io::Write;
        let dir = crate::paths::data_home().join("app-control");
        let _ = std::fs::create_dir_all(&dir);
        let line = format!(
            "{} {} id={} resident={} lease={} {}\n",
            chrono_stamp(),
            what,
            id,
            self.browsers.len(),
            self.leases.contains_key(id),
            extra
        );
        if let Ok(mut file) = std::fs::OpenOptions::new().create(true).append(true).open(dir.join("native-browser.log"))
        {
            let _ = file.write_all(line.as_bytes());
        }
    }

    /// 前端 → 原生内置浏览器桥。
    pub fn on_browser_command(&mut self, b: &Value) {
        let s = |key: &str| b.get(key).and_then(Value::as_str).unwrap_or("").to_string();
        let flag = |key: &str| b.get(key).and_then(Value::as_bool).unwrap_or(false);
        let id = s("id");
        match s("action").as_str() {
            "open" => {
                let hosts: Vec<String> = b
                    .get("allowedMainFrameHosts")
                    .and_then(Value::as_array)
                    .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
                    .unwrap_or_default();
                self.open_browser(OpenRequest {
                    id,
                    url: s("url"),
                    rect: b.get("rect").cloned().unwrap_or(Value::Null),
                    mode: s("nativeMode"),
                    chat_name: s("chatName"),
                    tag_name: s("tagName"),
                    isolated: flag("isolated"),
                    browser_tab: flag("browserTab"),
                    background: flag("background"),
                    allowed_hosts: hosts,
                });
            }
            "rect" => {
                if let Some(view) = self.browsers.get(&id) {
                    if !view.hidden {
                        apply_rect(view, b.get("rect").unwrap_or(&Value::Null));
                    }
                }
            }
            "hide" => {
                for view in self.browsers.values_mut() {
                    view.hidden = true;
                    let _ = view.webview.set_visible(false);
                }
            }
            "hideOne" => {
                if let Some(view) = self.browsers.get_mut(&id) {
                    view.hidden = true;
                    let _ = view.webview.set_visible(false);
                }
            }
            "tags" => {
                let items = b.get("items").and_then(Value::as_array).cloned().unwrap_or_default();
                self.render_tags(items);
            }
            "close" => {
                self.revoke_app_control(&id, "tab_closed");
                self.discard_browser_view(&id, "frontend_close");
            }
            "navigate" => {
                let url = s("url");
                if let Some(view) = self.browsers.get(&id) {
                    if !url.is_empty() {
                        let _ = view.webview.load_url(&url);
                    }
                }
            }
            "back" => {
                if let Some(view) = self.browsers.get(&id) {
                    let _ = view.webview.go_back();
                }
            }
            "forward" => {
                if let Some(view) = self.browsers.get(&id) {
                    let _ = view.webview.go_forward();
                }
            }
            "reload" => {
                if let Some(view) = self.browsers.get(&id) {
                    let _ = view.webview.reload();
                }
            }
            "larkContext" => self.read_lark_context(&id, &s("requestId")),
            "pickFolder" => self.pick_folder(),
            "agentDelegate" => self.toggle_app_control(&id, &s("sessionId")),
            "agentTakeover" => {
                let reason = b.get("reason").and_then(Value::as_str).unwrap_or("user_takeover").to_string();
                self.revoke_app_control(&id, &reason);
            }
            "agentPause" => {
                let reason = b.get("reason").and_then(Value::as_str).unwrap_or("user_interaction").to_string();
                self.pause_app_control(&id, &reason);
            }
            "agentResume" => self.resume_app_control(&id),
            // 窗口外观跟随页面主题：标题栏深浅色与主 WebView 底色。
            "appearance" => self.set_appearance(s("mode") == "light"),
            _ => {}
        }
    }

    pub fn open_browser(&mut self, request: OpenRequest) {
        let OpenRequest { id, url, rect, mode, chat_name, tag_name, isolated, browser_tab, background, allowed_hosts } =
            request;
        let hosts: HashSet<String> =
            allowed_hosts.iter().map(|h| h.trim_matches('.').to_ascii_lowercase()).filter(|h| !h.is_empty()).collect();
        let reuse = self.browsers.get(&id).map(|v| v.mode == mode).unwrap_or(false);
        let mut created = false;
        if reuse {
            self.browser_view_log(&id, "reuse", &format!("background={background}"));
            if let Some(view) = self.browsers.get(&id) {
                view.policy.borrow_mut().allowed_hosts = hosts;
            }
        } else {
            created = true;
            if self.browsers.contains_key(&id) {
                let old_mode = self.browsers.get(&id).map(|v| v.mode.clone()).unwrap_or_default();
                self.browser_view_log(&id, "rebuild", &format!("mode={old_mode}->{mode}"));
                self.browsers.remove(&id);
                self.contexts.remove(&id);
            }
            let policy = Rc::new(RefCell::new(ViewPolicy { allowed_hosts: hosts }));
            match self.create_browser_view(&id, &url, &mode, &chat_name, &tag_name, isolated, policy.clone()) {
                Ok(webview) => {
                    if isolated {
                        self.isolated.insert(id.clone());
                    }
                    self.browsers.insert(
                        id.clone(),
                        BrowserView { webview, mode: mode.clone(), hidden: true, last_used: Instant::now(), policy },
                    );
                    self.browser_view_log(&id, "create", &format!("background={background} isolated={isolated}"));
                }
                Err(error) => {
                    self.browser_view_log(&id, "create_failed", &error);
                    return;
                }
            }
        }
        if browser_tab {
            self.browser_tab_ids.insert(id.clone());
        }
        if let Some(view) = self.browsers.get_mut(&id) {
            view.last_used = Instant::now();
        }
        self.report_browser(&id, "state");
        if background {
            // 给真实尺寸再藏起来：零尺寸页面不布局，节点没有可用位置。
            if created {
                if let Some(view) = self.browsers.get(&id) {
                    let _ = view.webview.set_bounds(to_rect(0.0, 0.0, 1280.0, 800.0));
                    let _ = view.webview.set_visible(false);
                }
            }
            return;
        }
        for (key, view) in self.browsers.iter_mut() {
            if *key != id {
                view.hidden = true;
                let _ = view.webview.set_visible(false);
            }
        }
        if let Some(view) = self.browsers.get_mut(&id) {
            view.hidden = false;
            apply_rect(view, &rect);
            let _ = view.webview.set_visible(true);
            raise(view.webview.hwnd());
        }
        self.bring_tags_to_front();
        if is_lark_automation(&mode) {
            let script = lark_select_script(&chat_name, &tag_name, mode != "lark-search-chat");
            if let Some(view) = self.browsers.get(&id) {
                let _ = view.webview.evaluate_script(&script);
            }
        }
        self.trim_browser_views(&id, true);
    }

    #[allow(clippy::too_many_arguments)]
    fn create_browser_view(
        &mut self,
        id: &str,
        url: &str,
        mode: &str,
        chat_name: &str,
        tag_name: &str,
        isolated: bool,
        policy: Rc<RefCell<ViewPolicy>>,
    ) -> Result<wry::WebView, String> {
        let ipc_proxy = self.proxy.clone();
        let nav_proxy = self.proxy.clone();
        let window_proxy = self.proxy.clone();
        let title_proxy = self.proxy.clone();
        let download_proxy = self.proxy.clone();
        let done_proxy = self.proxy.clone();
        let (ipc_id, nav_id, window_id, title_id, download_id, done_id) =
            (id.to_string(), id.to_string(), id.to_string(), id.to_string(), id.to_string(), id.to_string());
        let parent = self.parent_hwnd();
        let nav_policy = policy.clone();
        let mut builder = WebViewBuilder::new_with_web_context(&mut self.context)
            .with_bounds(to_rect(0.0, 0.0, 1280.0, 800.0))
            .with_visible(false)
            .with_devtools(true)
            .with_incognito(isolated)
            .with_additional_browser_args(&self.browser_args)
            .with_background_throttling(BackgroundThrottlingPolicy::Disabled)
            .with_initialization_script_for_main_only(GUARD_SCRIPT, true)
            // 只记 fetch/XHR 在途数量与最后活动时间，供隔离世界判断网络空闲；各 frame 都注入。
            .with_initialization_script_for_main_only(NETWORK_TRACKER, false)
            .with_ipc_handler(move |request| {
                let _ = ipc_proxy.send_event(UserEvent::BrowserIpc { id: ipc_id.clone(), body: request.into_body() });
            })
            .with_navigation_handler(move |target| {
                if let Some(external) = external_target(&target) {
                    let _ = nav_proxy.send_event(UserEvent::ExternalScheme { id: nav_id.clone(), url: external });
                    return false;
                }
                let policy = nav_policy.borrow();
                if !policy.allowed_hosts.is_empty() {
                    let ok = target.to_ascii_lowercase().starts_with("https://")
                        && host_of(&target).map(|h| policy.allowed_hosts.contains(&h)).unwrap_or(false);
                    if !ok {
                        return false;
                    }
                }
                true
            })
            .with_new_window_req_handler(move |target, _| {
                // target=_blank / window.open：在同一面板就地打开，不弹新窗。
                let _ = window_proxy.send_event(UserEvent::BrowserOpen { id: window_id.clone(), url: target });
                NewWindowResponse::Deny
            })
            .with_document_title_changed_handler(move |_| {
                let _ = title_proxy.send_event(UserEvent::BrowserNav { id: title_id.clone(), phase: "state" });
            })
            .with_download_started_handler(move |source, path| {
                let _ = download_proxy.send_event(UserEvent::Download { id: download_id.clone(), phase: "started" });
                let suggested = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| {
                    source.rsplit('/').next().unwrap_or("").split(['?', '#']).next().unwrap_or("").to_string()
                });
                match dialogs::save_file_blocking(parent, &suggested) {
                    Some(target) => {
                        *path = target;
                        true
                    }
                    None => {
                        let _ = download_proxy
                            .send_event(UserEvent::Download { id: download_id.clone(), phase: "finished" });
                        false
                    }
                }
            })
            .with_download_completed_handler(move |_, _, _| {
                let _ = done_proxy.send_event(UserEvent::Download { id: done_id.clone(), phase: "finished" });
            });
        if is_lark_automation(mode) {
            builder = builder.with_initialization_script(at_document_end(&lark_select_script(
                chat_name,
                tag_name,
                mode != "lark-search-chat",
            )));
        }
        if is_lark_reference(mode) {
            builder = builder.with_initialization_script(at_document_end(LARK_REFERENCE));
        }
        if !url.is_empty() {
            builder = builder.with_url(url);
        }
        let webview = builder.build_as_child(&self.window).map_err(|e| e.to_string())?;
        let core = webview.webview();
        if is_chrome_ua(mode) {
            set_chrome_user_agent(&core);
        }
        self.attach_browser_events(id, &core);
        Ok(webview)
    }

    fn attach_browser_events(&self, id: &str, core: &ICoreWebView2) {
        let mut token = Default::default();
        let send = |phase: &'static str| {
            let proxy = self.proxy.clone();
            let id = id.to_string();
            move || {
                let _ = proxy.send_event(UserEvent::BrowserNav { id: id.clone(), phase });
            }
        };
        unsafe {
            let start = send("start");
            let _ = core.add_NavigationStarting(
                &NavigationStartingEventHandler::create(Box::new(move |_, _| {
                    start();
                    Ok(())
                })),
                &mut token,
            );
            let commit = send("commit");
            let _ = core.add_ContentLoading(
                &ContentLoadingEventHandler::create(Box::new(move |_, _| {
                    commit();
                    Ok(())
                })),
                &mut token,
            );
            let finish = send("finish");
            let fail = send("fail");
            let _ = core.add_NavigationCompleted(
                &NavigationCompletedEventHandler::create(Box::new(move |_, args| {
                    let ok = args
                        .map(|a| {
                            let mut success = windows::core::BOOL::default();
                            a.IsSuccess(&mut success).is_ok() && success.as_bool()
                        })
                        .unwrap_or(true);
                    if ok {
                        finish()
                    } else {
                        fail()
                    }
                    Ok(())
                })),
                &mut token,
            );
            // SPA 的 pushState、锚点与历史跳转不一定触发导航事件。
            let source = send("state");
            let _ = core.add_SourceChanged(
                &SourceChangedEventHandler::create(Box::new(move |_, _| {
                    source();
                    Ok(())
                })),
                &mut token,
            );
            let history = send("state");
            let _ = core.add_HistoryChanged(
                &HistoryChangedEventHandler::create(Box::new(move |_, _| {
                    history();
                    Ok(())
                })),
                &mut token,
            );
            self.attach_frame_scheme_guard(id, core);
        }
    }

    /// 隐藏 iframe 里的飞书 applink 与外部协议同样交给系统(macOS 在 decidePolicyFor 统一处理各 frame)。
    unsafe fn attach_frame_scheme_guard(&self, id: &str, core: &ICoreWebView2) {
        let proxy = self.proxy.clone();
        let id = id.to_string();
        let mut token = Default::default();
        let _ = core.add_FrameNavigationStarting(
            &NavigationStartingEventHandler::create(Box::new(move |_, args| {
                if let Some(args) = args {
                    let mut uri = PWSTR::null();
                    if args.Uri(&mut uri).is_ok() && !uri.is_null() {
                        let value = uri.to_string().unwrap_or_default();
                        windows::Win32::System::Com::CoTaskMemFree(Some(uri.0 as *const _));
                        if let Some(external) = external_target(&value) {
                            let _ = args.SetCancel(true);
                            let _ = proxy.send_event(UserEvent::ExternalScheme { id: id.clone(), url: external });
                        }
                    }
                }
                Ok(())
            })),
            &mut token,
        );
    }

    /// target=_blank / window.open 在同一面板就地打开；外部协议交给系统。
    pub fn on_browser_open(&mut self, id: &str, target: &str) {
        if let Some(external) = external_target(target) {
            self.handoff_external_scheme(id, &external);
        } else if let Some(view) = self.browsers.get(id) {
            let _ = view.webview.load_url(target);
        }
    }

    pub fn on_browser_nav(&mut self, id: &str, phase: &'static str) {
        if phase == "commit" {
            *self.epochs.entry(id.to_string()).or_insert(0) += 1;
            self.contexts.remove(id);
        }
        self.report_browser(id, phase);
    }

    /// 回传标题、地址、前进后退状态与加载阶段。
    pub fn report_browser(&mut self, id: &str, phase: &str) {
        let Some(view) = self.browsers.get(id) else { return };
        let url = view.webview.url().unwrap_or_default();
        let title = webview_title(&view.webview.webview());
        let back = view.webview.can_go_back().unwrap_or(false);
        let forward = view.webview.can_go_forward().unwrap_or(false);
        let payload = json!({"id": id, "title": title, "url": url, "canBack": back, "canFwd": forward, "phase": phase});
        self.eval_main(&format!("window.__browserEvent && window.__browserEvent({payload})"));
    }

    pub fn discard_browser_view(&mut self, id: &str, reason: &str) {
        self.browser_view_log(id, "discard", &format!("reason={}", if reason.is_empty() { "unknown" } else { reason }));
        self.revoke_agent_lease(id, if reason.is_empty() { "view_discarded" } else { reason });
        self.browsers.remove(id);
        self.browser_tab_ids.remove(id);
        self.epochs.remove(id);
        self.agent_leases.remove(id);
        self.isolated.remove(id);
        self.contexts.remove(id);
    }

    /// 切走的页面只隐藏；长期不用或驻留过多时才回收。委托中的页面不回收。
    pub fn trim_browser_views(&mut self, active: &str, enforce: bool) {
        let now = Instant::now();
        if !enforce && now.duration_since(self.browser_trimmed_at) < Duration::from_secs(60) {
            return;
        }
        self.browser_trimmed_at = now;
        let idle = Duration::from_secs(15 * 60);
        let resident_limit = 6;
        let eligible = |app: &App, id: &str| {
            id != active && !app.leases.contains_key(id) && app.browsers.get(id).map(|v| v.hidden).unwrap_or(false)
        };
        let stale: Vec<String> = self
            .browser_tab_ids
            .iter()
            .filter(|id| eligible(self, id))
            .filter(|id| self.browsers.get(*id).map(|v| now.duration_since(v.last_used) >= idle).unwrap_or(false))
            .cloned()
            .collect();
        for id in stale {
            self.discard_browser_view(&id, "trim_idle");
        }
        let mut resident = self.browser_tab_ids.iter().filter(|id| self.browsers.contains_key(*id)).count();
        if resident <= resident_limit {
            return;
        }
        let mut candidates: Vec<(Instant, String)> = self
            .browser_tab_ids
            .iter()
            .filter(|id| eligible(self, id) && !id.starts_with("agent-") && !id.starts_with("user-"))
            .filter_map(|id| self.browsers.get(id).map(|v| (v.last_used, id.clone())))
            .collect();
        candidates.sort();
        for (_, id) in candidates {
            if resident <= resident_limit {
                break;
            }
            self.discard_browser_view(&id, "trim_limit");
            resident -= 1;
        }
    }

    /// 主页面重载：原生面板全部藏起，标签条拆掉；视图与租约保留，前端重载后按 id 接回。
    pub fn park_native_overlays(&mut self) {
        let ids: Vec<String> = self.browsers.keys().cloned().collect();
        for id in ids {
            self.browser_view_log(&id, "park", "reason=host_navigation");
            if let Some(view) = self.browsers.get_mut(&id) {
                view.hidden = true;
                let _ = view.webview.set_visible(false);
            }
        }
        self.tags = None;
        self.tag_items.clear();
        self.tag_payload = Value::Null;
        self.contexts.remove("__main__");
        self.startup_log("main navigation committed");
    }

    pub fn on_browser_message(&mut self, id: &str, body: &str) {
        let Ok(message) = serde_json::from_str::<Value>(body) else { return };
        if message.get("name").and_then(Value::as_str) != Some("appControlUser") {
            return;
        }
        // 宿主经 DevTools 协议送的真实输入同样命中页面监听，这一小段时间不算人在操作。
        let native = self.native_input_until.get(id).map(|t| *t > Instant::now()).unwrap_or(false);
        if !native && self.browsers.contains_key(id) {
            self.pause_app_control(id, "user_interaction");
        }
    }

    pub fn on_download(&mut self, id: &str, phase: &str) {
        match phase {
            "started" => {
                if self.lease_of(id).is_some() {
                    self.pause_app_control(id, "file_download");
                }
            }
            _ => {
                if self.pause_reason.get(id).map(|r| r == "file_download").unwrap_or(false) {
                    self.resume_app_control(id);
                }
            }
        }
    }

    /// 非网页协议交给系统；Agent 隔离标签不拉起本机应用；未知协议先问人。
    pub fn handoff_external_scheme(&mut self, id: &str, url: &str) {
        let scheme = scheme_of(url);
        if self.isolated.contains(id) {
            self.browser_view_log(id, "external_scheme_blocked", &format!("scheme={scheme} isolated=true"));
            return;
        }
        if TRUSTED_APP_SCHEMES.contains(&scheme.as_str()) {
            self.browser_view_log(id, "external_scheme_open", &format!("scheme={scheme}"));
            winutil::shell_open(url);
            return;
        }
        let url = url.to_string();
        let log_id = id.to_string();
        let token = self.wait(Box::new(move |app, reply| {
            let allowed = matches!(reply, Reply::Dialog(dialogs::Answer::Confirmed(true)));
            app.browser_view_log(
                &log_id,
                "external_scheme_prompt",
                &format!("scheme={} allowed={allowed}", scheme_of(&url)),
            );
            if allowed {
                winutil::shell_open(&url);
            }
        }));
        dialogs::confirm(
            self.proxy.clone(),
            token,
            self.parent_hwnd(),
            "网页请求打开外部应用".into(),
            format!("当前网页想用「{scheme}:」打开本机应用。只在你信任该网页时允许。"),
            "打开".into(),
            "取消".into(),
        );
    }

    fn read_lark_context(&mut self, id: &str, request_id: &str) {
        let send = |app: &App, ok: bool, data: Option<Value>, error: &str| {
            let mut payload = json!({"requestId": request_id, "ok": ok});
            if let Some(data) = data {
                payload["data"] = data;
            }
            if !error.is_empty() {
                payload["error"] = Value::String(error.into());
            }
            app.eval_main(&format!("window.__larkRefContextResult && window.__larkRefContextResult({payload})"));
        };
        let ready =
            !request_id.is_empty() && self.browsers.get(id).map(|v| is_lark_reference(&v.mode)).unwrap_or(false);
        if !ready {
            send(self, false, None, "飞书原页尚未准备好");
            return;
        }
        let request_id = request_id.to_string();
        self.eval_with(Some(id), LARK_CONTEXT, move |app, result| {
            let send = |app: &App, ok: bool, data: Option<Value>, error: &str| {
                let mut payload = json!({"requestId": request_id, "ok": ok});
                if let Some(data) = data {
                    payload["data"] = data;
                }
                if !error.is_empty() {
                    payload["error"] = Value::String(error.into());
                }
                app.eval_main(&format!("window.__larkRefContextResult && window.__larkRefContextResult({payload})"));
            };
            match result {
                Err(error) => send(app, false, None, &error),
                Ok(row) if row.get("ok").and_then(Value::as_bool) == Some(true) => {
                    let data = json!({
                        "title": row.get("title").and_then(Value::as_str).unwrap_or("飞书聊天"),
                        "url": row.get("url").and_then(Value::as_str).unwrap_or(""),
                        "text": row.get("text").and_then(Value::as_str).unwrap_or(""),
                        "truncated": row.get("truncated").and_then(Value::as_bool).unwrap_or(false),
                    });
                    send(app, true, Some(data), "");
                }
                Ok(row) => {
                    let code = row.get("error").and_then(Value::as_str).unwrap_or("chat_unavailable");
                    let message = if code == "chat_not_focused" {
                        "飞书聊天尚未加载完成，请稍后重试"
                    } else {
                        "当前窗口没有可读取的聊天正文"
                    };
                    send(app, false, None, message);
                }
            }
        });
    }

    fn pick_folder(&mut self) {
        let token = self.wait(Box::new(|app, reply| {
            let path = match reply {
                Reply::Dialog(dialogs::Answer::Path(Some(path))) => path.to_string_lossy().into_owned(),
                _ => String::new(),
            };
            app.eval_main(&format!("window.__pickFolderCb && window.__pickFolderCb({})", js_string(&path)));
        }));
        dialogs::pick_folder(self.proxy.clone(), token, self.parent_hwnd());
    }

    // ------------------------------------------------------------------ 悬浮标签条

    /// 标签条：一个只覆盖各标签圆形区域的 WebView(窗口区域裁剪)，浮在浏览器面板之上。
    pub fn render_tags(&mut self, items: Vec<Value>) {
        if items.is_empty() {
            self.tags = None;
            self.tag_items.clear();
            self.tag_payload = Value::Null;
            return;
        }
        let num = |v: &Value, k: &str| v.get(k).and_then(Value::as_f64).unwrap_or(0.0);
        let (mut left, mut top, mut right, mut bottom) = (f64::MAX, f64::MAX, f64::MIN, f64::MIN);
        for item in &items {
            let (x, y, w, h) = (num(item, "x"), num(item, "y"), num(item, "w"), num(item, "h"));
            // 容器右上多留 5px 给悬出的关闭角标。
            left = left.min(x);
            top = top.min(y - 5.0);
            right = right.max(x + w + 5.0);
            bottom = bottom.max(y + h);
        }
        let bounds = to_rect(left, top, right - left, bottom - top);
        if self.tags.is_none() {
            let proxy = self.proxy.clone();
            let built = WebViewBuilder::new_with_web_context(&mut self.context)
                .with_bounds(bounds)
                .with_additional_browser_args(&self.browser_args)
                .with_background_color((0, 0, 0, 0))
                .with_transparent(true)
                .with_html(TAGS_PAGE)
                .with_ipc_handler(move |request| {
                    let _ = proxy.send_event(UserEvent::TagIpc { body: request.into_body() });
                })
                .build_as_child(&self.window);
            match built {
                Ok(view) => self.tags = Some(view),
                Err(_) => return,
            }
        }
        let payload = json!({"origin": {"x": left, "y": top}, "items": items});
        if let Some(tags) = &self.tags {
            let _ = tags.set_bounds(bounds);
            // 新建的标签页可能尚未加载完；页面就绪后会发 ready，再按 tag_payload 补渲染一次。
            let _ = tags.evaluate_script(&format!("window.__renderTags && window.__renderTags({payload})"));
        }
        self.tag_items = payload["items"].as_array().cloned().unwrap_or_default();
        self.tag_payload = payload;
        self.apply_tag_region(left, top);
        self.bring_tags_to_front();
    }

    fn apply_tag_region(&self, left: f64, top: f64) {
        let Some(tags) = &self.tags else { return };
        let scale = self.window.scale_factor();
        let num = |v: &Value, k: &str| v.get(k).and_then(Value::as_f64).unwrap_or(0.0);
        unsafe {
            let region = CreateRectRgn(0, 0, 0, 0);
            for item in &self.tag_items {
                let (x, y, w) = (num(item, "x") - left, num(item, "y") - top, num(item, "w"));
                let px = |v: f64| (v * scale).round() as i32;
                let circle = CreateEllipticRgn(px(x), px(y), px(x + w) + 1, px(y + w) + 1);
                let _ = CombineRgn(Some(region), Some(region), Some(circle), RGN_OR);
                let _ = DeleteObject(circle.into());
                let id = item.get("id").and_then(Value::as_str).unwrap_or("");
                if !id.is_empty() && id == self.tag_hover {
                    let badge = CreateRectRgn(px(x + w - 9.0), px(y - 5.0), px(x + w + 5.0), px(y + 9.0));
                    let _ = CombineRgn(Some(region), Some(region), Some(badge), RGN_OR);
                    let _ = DeleteObject(badge.into());
                }
            }
            // 系统接管区域对象，不再由调用方释放。
            SetWindowRgn(tags.hwnd(), Some(region), true);
        }
        let _ = RECT::default();
    }

    pub fn bring_tags_to_front(&self) {
        if let Some(tags) = &self.tags {
            raise(tags.hwnd());
        }
    }

    pub fn on_tag_message(&mut self, body: &str) {
        let Ok(message) = serde_json::from_str::<Value>(body) else { return };
        let id = message.get("id").and_then(Value::as_str).unwrap_or("").to_string();
        match message.get("type").and_then(Value::as_str).unwrap_or("") {
            "ready" => {
                if let (Some(tags), false) = (&self.tags, self.tag_payload.is_null()) {
                    let payload = &self.tag_payload;
                    let _ = tags.evaluate_script(&format!("window.__renderTags && window.__renderTags({payload})"));
                }
            }
            "click" => self.eval_main(&format!("window.__tagClick && window.__tagClick({})", js_string(&id))),
            "close" => self.eval_main(&format!("window.__tagClose && window.__tagClose({})", js_string(&id))),
            "hover" => {
                if self.tag_hover != id {
                    self.tag_hover = id;
                    let num = |v: &Value, k: &str| v.get(k).and_then(Value::as_f64).unwrap_or(0.0);
                    let left = self.tag_items.iter().map(|i| num(i, "x")).fold(f64::MAX, f64::min);
                    let top = self.tag_items.iter().map(|i| num(i, "y") - 5.0).fold(f64::MAX, f64::min);
                    self.apply_tag_region(left, top);
                }
            }
            _ => {}
        }
    }
}

pub struct OpenRequest {
    pub id: String,
    pub url: String,
    pub rect: Value,
    pub mode: String,
    pub chat_name: String,
    pub tag_name: String,
    pub isolated: bool,
    pub browser_tab: bool,
    pub background: bool,
    pub allowed_hosts: Vec<String>,
}

fn apply_rect(view: &BrowserView, rect: &Value) {
    let Some((x, y, w, h)) = rect_from(rect) else { return };
    // 前端上报左缘热区时，让出这一条给主页面的分隔条拖动。
    let pass = rect.get("passThroughLeft").and_then(Value::as_f64).unwrap_or(0.0).clamp(0.0, w);
    let _ = view.webview.set_bounds(to_rect(x + pass, y, w - pass, h));
}

/// Chrome 模式：去掉 WebView2 UA 里的 Edg/ 标记，让站点走 Chrome 分支(对应 macOS 的 chromeUA)。
fn set_chrome_user_agent(core: &ICoreWebView2) {
    unsafe {
        let Ok(settings) = core.Settings() else { return };
        let Ok(settings) = settings.cast::<ICoreWebView2Settings2>() else { return };
        let mut current = PWSTR::null();
        if settings.UserAgent(&mut current).is_err() || current.is_null() {
            return;
        }
        let value = current.to_string().unwrap_or_default();
        windows::Win32::System::Com::CoTaskMemFree(Some(current.0 as *const _));
        let chrome: Vec<&str> = value.split(' ').filter(|part| !part.starts_with("Edg/")).collect();
        let ua = windows::core::HSTRING::from(chrome.join(" "));
        let _ = settings.SetUserAgent(&ua);
    }
}

fn chrono_stamp() -> String {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
    let secs = now.as_secs();
    format!("{:02}:{:02}:{:02}.{:03}", (secs / 3600) % 24, (secs / 60) % 60, secs % 60, now.subsec_millis())
}
