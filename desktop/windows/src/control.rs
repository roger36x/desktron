//! App Control：宿主委托、取活轮询与 WebView2 适配器。语义与 macOS 壳逐项一致：
//! - 委托租约只驻留原生宿主；人碰被委托页面立即让开(暂停)，静默 4 秒后自动接回；
//! - 页面脚本在隔离世界执行，页面 JS 看不到也改不了；
//! - 点击、悬停、拖拽与按键经 DevTools 协议送达，页面收到 isTrusted=true 的事件；
//! - 回执先落库，再执行可能撤销租约的动作(close / handoff)。

use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};
use wry::WebViewExtWindows;

use crate::app::{App, Reply};
use crate::cdp;

const DOM_SCRIPT: &str = include_str!("../../shared/js/app-control-dom.js");
const DESK_SURFACE: &str = include_str!("../../shared/js/desk-surface.js");
const PAGE_SETTLED: &str = include_str!("../js/page-settled.js");
const RESUME_QUIET: Duration = Duration::from_secs(4);
const MAIN: &str = "__main__";

type Done = Box<dyn FnOnce(&mut App, Result<Value, String>)>;
type SettledDone = Box<dyn FnOnce(&mut App, Option<bool>)>;
type ViewDone = Box<dyn FnOnce(&mut App, bool)>;

fn get_str<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or("")
}

fn obj(value: &Value) -> Map<String, Value> {
    value.as_object().cloned().unwrap_or_default()
}

impl App {
    // ------------------------------------------------------------------ DevTools 协议

    fn core_for(&self, target: &str) -> Option<webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2> {
        if target == MAIN {
            Some(self.web.webview())
        } else {
            self.browsers.get(target).map(|v| v.webview.webview())
        }
    }

    pub fn cdp(
        &mut self,
        target: &str,
        method: &str,
        params: Value,
        done: impl FnOnce(&mut App, Result<Value, String>) + 'static,
    ) {
        let token = self.wait(Box::new(move |app, reply| {
            if let Reply::Value(result) = reply {
                done(app, result);
            }
        }));
        match self.core_for(target) {
            Some(core) => cdp::call(&core, &self.proxy, token, method, params),
            None => {
                let _ = self
                    .proxy
                    .send_event(crate::app::UserEvent::Cdp { token, result: Err("browser_tab_unavailable".into()) });
            }
        }
    }

    /// 依次执行一串协议调用；任一失败即停止。
    fn cdp_sequence(&mut self, target: String, mut calls: Vec<(&'static str, Value)>, done: Done) {
        if calls.is_empty() {
            done(self, Ok(Value::Null));
            return;
        }
        let (method, params) = calls.remove(0);
        self.cdp(&target.clone(), method, params, move |app, result| match result {
            Err(error) => done(app, Err(error)),
            Ok(_) => app.cdp_sequence(target, calls, done),
        });
    }

    /// 在隔离世界执行表达式(对应 WKContentWorld)。导航后上下文失效时重建一次。
    pub fn eval_isolated(&mut self, target: &str, expression: String, done: Done) {
        self.eval_isolated_attempt(target.to_string(), expression, done, true);
    }

    fn eval_isolated_attempt(&mut self, target: String, expression: String, done: Done, retry: bool) {
        if let Some(context) = self.contexts.get(&target).copied() {
            let params = cdp::evaluate_params(&expression, context);
            self.cdp(&target.clone(), "Runtime.evaluate", params, move |app, result| match result {
                Ok(value) => done(app, cdp::evaluate_result(&value)),
                Err(error) => {
                    app.contexts.remove(&target);
                    if retry {
                        app.eval_isolated_attempt(target, expression, done, false);
                    } else {
                        done(app, Err(error));
                    }
                }
            });
            return;
        }
        self.cdp(&target.clone(), "Page.getFrameTree", json!({}), move |app, result| {
            let frame = match result {
                Ok(tree) => tree["frameTree"]["frame"]["id"].as_str().unwrap_or("").to_string(),
                Err(error) => return done(app, Err(error)),
            };
            if frame.is_empty() {
                return done(app, Err("frame_unavailable".into()));
            }
            app.cdp(
                &target.clone(),
                "Page.createIsolatedWorld",
                cdp::create_world_params(&frame),
                move |app, result| {
                    let context = result.ok().and_then(|v| v["executionContextId"].as_i64());
                    match context {
                        Some(context) => {
                            app.contexts.insert(target.clone(), context);
                            app.eval_isolated_attempt(target, expression, done, false);
                        }
                        None => done(app, Err("isolated_world_unavailable".into())),
                    }
                },
            );
        });
    }

    fn run_dom_script(&mut self, target: &str, command: Value, epoch: i64, done: Done) {
        let expression = cdp::async_function_expression(DOM_SCRIPT, &["command", "epoch"], &[command, json!(epoch)]);
        self.eval_isolated(target, expression, done);
    }

    // ------------------------------------------------------------------ 租约状态

    pub fn lease_of(&self, id: &str) -> Option<String> {
        self.leases.get(id).or_else(|| self.agent_leases.get(id)).cloned()
    }

    pub fn send_app_control_event(&self, id: &str, kind: &str, lease: &str, error: &str, session: &str) {
        let desk = id.starts_with("desk-window:");
        let bound = if desk && session.is_empty() { self.desk_session_id.clone() } else { session.to_string() };
        let value = json!({"id": id, "surface": if desk { "navigation" } else { "" }, "type": kind,
            "lease_id": lease, "error": error, "session_id": bound,
            "interaction_epoch": if desk { self.desk_generation } else { 0 }});
        let receiver = if desk { "__deskControlEvent" } else { "__browserControlEvent" };
        self.eval_main(&format!("window.{receiver} && window.{receiver}({value})"));
    }

    pub fn delegate_desk_control(&mut self, session: &str) {
        if session.is_empty() || !self.desk_resource_id.is_empty() {
            return;
        }
        let resource = format!("desk-window:{}:navigation", self.desk_window_id);
        self.desk_generation += 1;
        let generation = self.desk_generation;
        let session = session.to_string();
        let body = json!({"app_id": "desk", "resource_id": resource, "window_id": self.desk_window_id,
            "session_id": session, "ttl_seconds": 900});
        self.backend("/workbench/app-control/host/delegate", "POST", Some(body), move |app, result, error| {
            let lease = result.as_ref().and_then(|v| v.get("id")).and_then(Value::as_str).map(str::to_string);
            if generation != app.desk_generation {
                if let Some(lease) = lease {
                    app.backend(
                        "/workbench/app-control/host/takeover",
                        "POST",
                        Some(json!({"lease_id": lease, "reason": "resource_changed"})),
                        |_, _, _| {},
                    );
                }
                return;
            }
            match (error, lease) {
                (None, Some(lease)) => {
                    app.desk_resource_id = resource.clone();
                    app.desk_session_id = session.clone();
                    app.leases.insert(resource.clone(), lease.clone());
                    app.send_app_control_event(&resource, "delegated", &lease, "", &session);
                    app.ensure_app_control_timer();
                }
                (error, _) => {
                    let message = error.unwrap_or_else(|| "委托失败".into());
                    app.send_app_control_event(&resource, "error", "", &message, &session);
                }
            }
        });
    }

    pub fn revoke_desk_control(&mut self, reason: &str) {
        self.desk_generation += 1;
        let resource = self.desk_resource_id.clone();
        if resource.is_empty() {
            return;
        }
        self.revoke_app_control(&resource, reason);
        self.desk_resource_id.clear();
        self.desk_session_id.clear();
    }

    pub fn toggle_app_control(&mut self, id: &str, session: &str) {
        if self.leases.contains_key(id) {
            self.revoke_app_control(id, "user_takeover");
            return;
        }
        if !self.browsers.contains_key(id) {
            self.send_app_control_event(id, "error", "", "页面尚未加载", "");
            return;
        }
        let window_id = format!("window-{}", self.parent_hwnd());
        let body = json!({"app_id": "browser", "resource_id": id, "window_id": window_id,
            "session_id": session, "ttl_seconds": 900});
        let id = id.to_string();
        self.backend("/workbench/app-control/host/delegate", "POST", Some(body), move |app, result, error| {
            let lease = result.as_ref().and_then(|v| v.get("id")).and_then(Value::as_str).map(str::to_string);
            match (error, lease) {
                (None, Some(lease)) => {
                    app.leases.insert(id.clone(), lease.clone());
                    app.send_app_control_event(&id, "delegated", &lease, "", "");
                    app.ensure_app_control_timer();
                }
                (error, _) => {
                    let message = error.unwrap_or_else(|| "委托失败".into());
                    app.send_app_control_event(&id, "error", "", &message, "");
                }
            }
        });
    }

    /// 人碰了被委托的页面：让开但不收回；静默够久自动恢复。
    pub fn pause_app_control(&mut self, id: &str, reason: &str) {
        let Some(lease) = self.lease_of(id) else { return };
        self.last_touch.insert(id.to_string(), Instant::now());
        if self.paused.contains(id)
            && (reason != "tab_switched" || self.pause_reason.get(id).map(String::as_str) == Some(reason))
        {
            return;
        }
        self.paused.insert(id.to_string());
        self.pause_reason.insert(id.to_string(), reason.to_string());
        self.send_app_control_event(id, "paused", &lease, reason, "");
        self.backend(
            "/workbench/app-control/host/pause",
            "POST",
            Some(json!({"lease_id": lease, "reason": reason})),
            |_, _, _| {},
        );
        self.ensure_app_control_timer();
    }

    fn resume_idle_app_control(&mut self) {
        let ids: Vec<String> = self.paused.iter().cloned().collect();
        for id in ids {
            let Some(lease) = self.lease_of(&id) else {
                self.paused.remove(&id);
                continue;
            };
            if self.pause_reason.get(&id).map(String::as_str).unwrap_or("user_interaction") != "user_interaction" {
                continue;
            }
            let quiet = self.last_touch.get(&id).map(|t| t.elapsed() >= RESUME_QUIET).unwrap_or(true);
            if !quiet {
                continue;
            }
            self.paused.remove(&id);
            self.pause_reason.remove(&id);
            self.resume_request(id, lease);
        }
    }

    pub fn resume_app_control(&mut self, id: &str) {
        if !self.paused.contains(id) {
            return;
        }
        let Some(lease) = self.lease_of(id) else { return };
        self.paused.remove(id);
        self.pause_reason.remove(id);
        self.resume_request(id.to_string(), lease);
    }

    fn resume_request(&mut self, id: String, lease: String) {
        self.backend(
            "/workbench/app-control/host/resume",
            "POST",
            Some(json!({"lease_id": lease, "ttl_seconds": 900})),
            move |app, _, error| {
                if let Some(error) = error {
                    // 恢复不了(多半已被撤或过期)：按撤销收口，别让界面停在“暂停”。
                    app.leases.remove(&id);
                    app.agent_leases.remove(&id);
                    app.send_app_control_event(&id, "revoked", "", &error, "");
                    if id == app.desk_resource_id {
                        app.desk_resource_id.clear();
                        app.desk_session_id.clear();
                    }
                    return;
                }
                app.send_app_control_event(&id, "delegated", &lease, "", "");
            },
        );
    }

    pub fn revoke_agent_lease(&mut self, id: &str, reason: &str) {
        let Some(lease) = self.agent_leases.remove(id) else { return };
        self.browser_view_log(id, "agent_lease_revoked", &format!("reason={reason} lease={lease}"));
        self.backend(
            "/workbench/app-control/host/takeover",
            "POST",
            Some(json!({"lease_id": lease, "reason": reason})),
            |_, _, _| {},
        );
    }

    pub fn revoke_app_control(&mut self, id: &str, reason: &str) {
        self.pause_reason.remove(id);
        self.paused.remove(id);
        self.last_touch.remove(id);
        let Some(lease) = self.leases.remove(id) else { return };
        self.send_app_control_event(id, "revoked", &lease, reason, "");
        self.backend(
            "/workbench/app-control/host/takeover",
            "POST",
            Some(json!({"lease_id": lease, "reason": reason})),
            |_, _, _| {},
        );
        if self.leases.is_empty() {
            self.paused.clear();
            self.pause_reason.clear();
            self.last_touch.clear();
            self.ensure_app_control_timer();
        }
    }

    pub fn replay_app_control_state(&mut self) {
        let leases: Vec<(String, String)> = self.leases.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
        for (id, lease) in leases {
            let paused = self.paused.contains(&id);
            let reason = if paused { self.pause_reason.get(&id).cloned().unwrap_or_default() } else { String::new() };
            let session = if id == self.desk_resource_id { self.desk_session_id.clone() } else { String::new() };
            self.send_app_control_event(&id, if paused { "paused" } else { "delegated" }, &lease, &reason, &session);
        }
    }

    /// 能力上报：Windows 没有 macOS 原生应用观察实现，状态为 unsupported。
    pub fn report_desktop_capability(&mut self, force: bool) {
        let fresh = self.capability_reported_at.map(|t| t.elapsed() < Duration::from_secs(10)).unwrap_or(false);
        if !force && fresh {
            return;
        }
        let Some(host) = self.supervisor.as_ref().map(|s| s.host_instance_id.clone()) else { return };
        let checked =
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
        let body = json!({"host_instance_id": host, "host_bundle_id": "io.github.desktron",
            "host_pid": std::process::id(), "status": "unsupported", "checked_at": checked,
            "monitoring_status": "unavailable"});
        self.backend("/workbench/app-control/host/desktop-capability", "POST", Some(body), |app, _, error| {
            if let Some(error) = error {
                app.startup_log(&format!("desktop capability report failed: {error}"));
            } else {
                app.capability_reported_at = Some(Instant::now());
            }
        });
    }

    // ------------------------------------------------------------------ 取活循环

    /// 常驻取活：空闲 1.5 秒一拍，手上有租约时 0.35 秒一拍。
    pub fn ensure_app_control_timer(&mut self) {
        let want = if self.leases.is_empty() { Duration::from_millis(1500) } else { Duration::from_millis(350) };
        if self.poll_timer != 0 && self.poll_interval == want {
            return;
        }
        if self.poll_timer != 0 {
            self.cancel_timer(self.poll_timer);
        }
        self.poll_interval = want;
        self.schedule_poll();
    }

    fn schedule_poll(&mut self) {
        let interval = self.poll_interval;
        self.poll_timer = self.after(interval, |app| {
            app.poll_timer = 0;
            app.poll_app_control();
            if app.poll_timer == 0 {
                app.schedule_poll();
            }
        });
    }

    fn poll_app_control(&mut self) {
        self.resume_idle_app_control();
        self.trim_browser_views("", false);
        if self.polling {
            return;
        }
        self.polling = true;
        let apps = ["browser", "desk"];
        let app_id = apps[self.poll_index % apps.len()];
        self.poll_index = (self.poll_index + 1) % apps.len();
        let path = format!("/workbench/app-control/host/next?app_id={app_id}");
        self.backend(&path, "GET", None, move |app, next, error| {
            app.polling = false;
            if error.is_some() {
                return;
            }
            if let Some(operation) = next.and_then(|v| v.get("operation").cloned()).filter(Value::is_object) {
                if app_id == "desk" {
                    app.execute_desk_app_control(operation);
                } else {
                    app.execute_app_control(operation);
                }
            }
        });
    }

    fn op_handle(operation: &Value) -> String {
        let id = get_str(operation, "id");
        let ticket = get_str(operation, "dispatch_id");
        if ticket.is_empty() {
            id.to_string()
        } else {
            format!("{id}.{ticket}")
        }
    }

    pub fn finish_app_control(&mut self, op: &str, ok: bool, output: Option<Value>, error: &str, version: &str) {
        self.finish_app_control_then(op, ok, output, error, version, |_, _| {});
    }

    fn finish_app_control_then(
        &mut self,
        op: &str,
        ok: bool,
        output: Option<Value>,
        error: &str,
        version: &str,
        completion: impl FnOnce(&mut App, bool) + 'static,
    ) {
        let (operation, dispatch) = match op.split_once('.') {
            Some((a, b)) => (a.to_string(), Some(b.to_string())),
            None => (op.to_string(), None),
        };
        let mut body = json!({"operation_id": operation, "ok": ok, "error": error, "observation_version": version});
        if let Some(dispatch) = dispatch {
            body["dispatch_id"] = Value::String(dispatch);
        }
        if let Some(output) = output {
            body["output"] = output;
        }
        self.backend("/workbench/app-control/host/result", "POST", Some(body), move |app, _, error| {
            completion(app, error.is_none());
        });
    }

    // ------------------------------------------------------------------ Desk 导航面

    fn desk_operation_valid(&self, operation: &Value, epoch: i64) -> bool {
        let resource = get_str(operation, "resource_id");
        epoch == self.desk_generation
            && resource == self.desk_resource_id
            && self.leases.get(resource).map(String::as_str) == Some(get_str(operation, "lease_id"))
            && get_str(operation, "session_id") == self.desk_session_id
            && !self.paused.contains(resource)
    }

    fn execute_desk_app_control(&mut self, operation: Value) {
        let op = Self::op_handle(&operation);
        let resource = get_str(&operation, "resource_id").to_string();
        let valid = !resource.is_empty()
            && resource == self.desk_resource_id
            && self.leases.get(&resource).map(String::as_str) == Some(get_str(&operation, "lease_id"))
            && get_str(&operation, "session_id") == self.desk_session_id
            && !self.paused.contains(&resource);
        if !valid {
            self.finish_app_control(&op, false, None, "desk_surface_unavailable", "");
            return;
        }
        let kind = get_str(&operation, "kind").to_string();
        let name = get_str(&operation, "name").to_string();
        let epoch = self.desk_generation;
        match (kind.as_str(), name.as_str()) {
            ("inspect", "surface") => self.snapshot_desk_surface(op, operation, epoch),
            ("inspect", "screenshot") => {
                let input = operation.get("input").cloned().unwrap_or(json!({}));
                self.snapshot(op, MAIN.to_string(), input, true, Some((operation, epoch)));
            }
            ("act", "open_page") | ("act", "toggle_panel") => self.run_desk_semantic_action(op, operation, epoch),
            _ => self.finish_app_control(&op, false, None, "unsupported_desk_operation", ""),
        }
    }

    fn run_desk_semantic_action(&mut self, op: String, operation: Value, epoch: i64) {
        if !self.desk_operation_valid(&operation, epoch) {
            return self.finish_app_control(&op, false, None, "desk_surface_unavailable", "");
        }
        let payload = json!({"action": get_str(&operation, "name"), "session_id": self.desk_session_id,
            "interaction_epoch": epoch, "input": operation.get("input").cloned().unwrap_or(json!({}))});
        let js = format!("window.__deskAgentCommand && window.__deskAgentCommand({payload})");
        self.eval_with(None, &js, move |app, result| {
            if !app.desk_operation_valid(&operation, epoch) {
                return app.finish_app_control(&op, false, None, "human_intervention_reinspect_required", "");
            }
            match result {
                Ok(Value::Bool(true)) => {
                    app.after(Duration::from_millis(100), move |app| {
                        if !app.desk_operation_valid(&operation, epoch) {
                            return app.finish_app_control(
                                &op,
                                false,
                                None,
                                "human_intervention_reinspect_required",
                                "",
                            );
                        }
                        app.snapshot_desk_surface(op, operation, epoch);
                    });
                }
                Ok(_) => app.finish_app_control(&op, false, None, "desk_action_rejected", ""),
                Err(error) => app.finish_app_control(&op, false, None, &error, ""),
            }
        });
    }

    fn snapshot_desk_surface(&mut self, op: String, operation: Value, epoch: i64) {
        if !self.desk_operation_valid(&operation, epoch) {
            return self.finish_app_control(&op, false, None, "human_intervention_reinspect_required", "");
        }
        let expression = cdp::async_function_expression(DESK_SURFACE, &[], &[]);
        self.eval_isolated(
            MAIN,
            expression,
            Box::new(move |app, result| {
                if !app.desk_operation_valid(&operation, epoch) {
                    return app.finish_app_control(&op, false, None, "human_intervention_reinspect_required", "");
                }
                match result {
                    Ok(row) if row.is_object() => {
                        let ok = row.get("ok").and_then(Value::as_bool) == Some(true);
                        app.finish_app_control(
                            &op,
                            ok,
                            row.get("output").cloned(),
                            get_str(&row, "error"),
                            get_str(&row, "observation_version"),
                        );
                    }
                    Ok(_) => app.finish_app_control(&op, false, None, "invalid_desk_snapshot", ""),
                    Err(error) => app.finish_app_control(&op, false, None, &error, ""),
                }
            }),
        );
    }

    // ------------------------------------------------------------------ 内置浏览器适配器

    fn execute_app_control(&mut self, operation: Value) {
        let op = Self::op_handle(&operation);
        let resource = get_str(&operation, "resource_id").to_string();
        if get_str(&operation, "id").is_empty() || resource.is_empty() {
            return;
        }
        let lease = get_str(&operation, "lease_id");
        if !self.leases.contains_key(&resource) && !lease.is_empty() {
            self.agent_leases.insert(resource.clone(), lease.to_string());
        }
        let kind = get_str(&operation, "kind").to_string();
        let name = get_str(&operation, "name").to_string();
        let input = operation.get("input").cloned().unwrap_or(json!({}));
        // open 在视图存在之前就放行——它就是去建那个视图的。
        if kind == "act" && name == "open" {
            return self.open_agent_tab(op, resource, input);
        }
        if !self.browsers.contains_key(&resource) {
            self.browser_view_log(&resource, "miss", &format!("op={name}"));
            return self.finish_app_control(&op, false, None, "browser_tab_unavailable", "");
        }
        if let Some(view) = self.browsers.get_mut(&resource) {
            view.last_used = Instant::now();
        }
        match (kind.as_str(), name.as_str()) {
            ("inspect", "screenshot") => return self.snapshot(op, resource, input, false, None),
            ("act", "navigate") => {
                let raw = get_str(&input, "url").to_string();
                let lower = raw.to_ascii_lowercase();
                if !(lower.starts_with("http://") || lower.starts_with("https://")) {
                    return self.finish_app_control(&op, false, None, "only_http_https_navigation_allowed", "");
                }
                let epoch = self.bump_epoch(&resource);
                if let Some(view) = self.browsers.get(&resource) {
                    let _ = view.webview.load_url(&raw);
                }
                return self.finish_app_control(
                    &op,
                    true,
                    Some(json!({"url": raw})),
                    "",
                    &format!("{epoch}:navigation"),
                );
            }
            ("act", "back") | ("act", "forward") | ("act", "reload") => {
                let Some(view) = self.browsers.get(&resource) else { return };
                if name == "back" && !view.webview.can_go_back().unwrap_or(false) {
                    return self.finish_app_control(&op, false, None, "no_back_history", "");
                }
                if name == "forward" && !view.webview.can_go_forward().unwrap_or(false) {
                    return self.finish_app_control(&op, false, None, "no_forward_history", "");
                }
                let _ = match name.as_str() {
                    "back" => view.webview.go_back(),
                    "forward" => view.webview.go_forward(),
                    _ => view.webview.reload(),
                };
                let url = view.webview.url().unwrap_or_default();
                let epoch = self.bump_epoch(&resource);
                return self.finish_app_control(
                    &op,
                    true,
                    Some(json!({"navigation": name, "url": url})),
                    "",
                    &format!("{epoch}:navigation"),
                );
            }
            ("act", "close") => {
                self.finish_app_control(&op, true, Some(json!({"tab": resource, "action": name})), "", "");
                self.after(Duration::from_millis(100), move |app| {
                    app.run_browser_agent_command("close", &resource, |_, _| {})
                });
                return;
            }
            ("act", "activate") => {
                let id = resource.clone();
                self.run_browser_agent_command("activate", &resource, move |app, ok| {
                    let output = ok.then(|| {
                        json!({"tab": id, "action": "activate", "presentation": "background",
                        "user_tab_unchanged": true})
                    });
                    app.finish_app_control(&op, ok, output, if ok { "" } else { "browser_tab_command_failed" }, "");
                });
                return;
            }
            ("act", "handoff") => {
                let id = resource.clone();
                self.run_browser_agent_command("handoff", &resource, move |app, ok| {
                    if !ok {
                        return app.finish_app_control(&op, false, None, "browser_tab_command_failed", "");
                    }
                    let output = json!({"resource_id": id, "action": "handoff", "presentation": "user_foreground",
                        "same_tab": true, "requires_reinspect": true});
                    app.finish_app_control_then(&op, true, Some(output), "", "", move |app, stored| {
                        if stored {
                            app.run_browser_agent_command("takeover", &id, |_, _| {});
                        }
                    });
                });
                return;
            }
            ("act", "click" | "coordinate_click" | "hover" | "dblclick" | "check" | "uncheck") => {
                return self.run_native_pointer(op, resource, name, input);
            }
            ("act", "drag") => return self.run_native_drag(op, resource, input),
            ("act", "press" | "type") => {
                if let Some(keys) = cdp::key_events(&name, &input) {
                    return self.run_native_input(op, resource, input, keys);
                }
            }
            _ => {}
        }
        let epoch = self.epochs.get(&resource).copied().unwrap_or(0);
        let command = json!({"kind": kind, "name": name, "input": input});
        let settle_ms = input.get("settle_ms").and_then(Value::as_f64).unwrap_or(3000.0);
        let budget = (settle_ms / 1000.0).clamp(0.0, 10.0);
        let run = move |app: &mut App, settled: Option<bool>| {
            let id = resource.clone();
            let kind2 = kind.clone();
            let name2 = name.clone();
            app.run_dom_script(
                &resource,
                command,
                epoch,
                Box::new(move |app, result| match result {
                    Ok(row) if row.is_object() => {
                        let mut out = row.get("output").cloned();
                        if kind2 == "inspect" && name2 == "document" {
                            if let Some(Value::Object(d)) = out.as_mut() {
                                let hidden = app.browsers.get(&id).map(|v| v.hidden).unwrap_or(true);
                                d.insert("resource_id".into(), json!(id));
                                d.insert("view_hidden".into(), json!(hidden));
                                d.insert("presentation".into(), json!(if hidden { "background" } else { "visible" }));
                            }
                        }
                        if let (Some(settled), Some(Value::Object(d))) = (settled, out.as_mut()) {
                            d.insert("settled".into(), json!(settled));
                        }
                        let ok = row.get("ok").and_then(Value::as_bool) == Some(true);
                        app.finish_app_control(
                            &op,
                            ok,
                            out,
                            get_str(&row, "error"),
                            get_str(&row, "observation_version"),
                        );
                    }
                    Ok(_) => app.finish_app_control(&op, false, None, "invalid_adapter_result", ""),
                    Err(error) => {
                        let error = if name2 == "wait" { "wait_context_changed".to_string() } else { error };
                        app.finish_app_control(&op, false, None, &error, "");
                    }
                }),
            );
        };
        if budget > 0.0 && operation.get("kind").and_then(Value::as_str) == Some("inspect") {
            let target = get_str(&operation, "resource_id").to_string();
            self.wait_for_page_settled(target, Instant::now() + Duration::from_secs_f64(budget), None, Box::new(run));
        } else {
            run(self, None);
        }
    }

    fn bump_epoch(&mut self, id: &str) -> i64 {
        let entry = self.epochs.entry(id.to_string()).or_insert(0);
        *entry += 1;
        *entry
    }

    /// 排除初始空白页，等到 complete 且地址/节点数连续两次相同；超时照读并标记 settled=false。
    fn wait_for_page_settled(&mut self, target: String, deadline: Instant, last: Option<String>, done: SettledDone) {
        if Instant::now() >= deadline {
            return done(self, Some(false));
        }
        let expression = PAGE_SETTLED.to_string();
        self.eval_isolated(
            &target.clone(),
            expression,
            Box::new(move |app, result| {
                let n = result.ok().and_then(|v| v.as_str().map(str::to_string));
                if n.is_some() && n == last {
                    return done(app, Some(true));
                }
                app.after(Duration::from_millis(200), move |app| app.wait_for_page_settled(target, deadline, n, done));
            }),
        );
    }

    /// Agent 自开标签：前端把它加进标签条再经既有 open 通道建视图；等视图真的出现再报成功。
    fn open_agent_tab(&mut self, op: String, resource: String, input: Value) {
        let url = get_str(&input, "url").to_string();
        let isolated = input.get("isolated").and_then(Value::as_bool).unwrap_or(true);
        let lower = url.to_ascii_lowercase();
        if !(lower.starts_with("http://") || lower.starts_with("https://")) {
            return self.finish_app_control(&op, false, None, "only_http_https_allowed", "");
        }
        let hosts = input.get("allowed_main_frame_hosts").cloned().unwrap_or(json!([]));
        let payload = json!({"id": resource, "url": url, "isolated": isolated, "allowedMainFrameHosts": hosts});
        let js = format!("window.__browserAgentOpen && window.__browserAgentOpen({payload})");
        self.eval_with(None, &js, move |app, result| match result {
            Err(error) => app.finish_app_control(&op, false, None, &error, ""),
            Ok(Value::Bool(true)) => app.wait_for_browser_view(
                resource.clone(),
                40,
                Box::new(move |app, ok| {
                    if !ok {
                        return app.finish_app_control(&op, false, None, "browser_view_not_created", "");
                    }
                    app.epochs.insert(resource.clone(), 1);
                    app.finish_app_control(
                        &op,
                        true,
                        Some(json!({"url": url, "isolated": isolated,
                    "presentation": "background", "user_tab_unchanged": true})),
                        "",
                        "1:open",
                    );
                }),
            ),
            Ok(_) => app.finish_app_control(&op, false, None, "browser_app_not_open", ""),
        });
    }

    fn wait_for_browser_view(&mut self, id: String, tries: u32, done: ViewDone) {
        if self.browsers.contains_key(&id) {
            return done(self, true);
        }
        if tries == 0 {
            return done(self, false);
        }
        self.after(Duration::from_millis(150), move |app| app.wait_for_browser_view(id, tries - 1, done));
    }

    fn still_owned(&self, resource: &str, touch: Option<Instant>) -> bool {
        !self.paused.contains(resource)
            && self.lease_of(resource).is_some()
            && self.last_touch.get(resource).copied() == touch
    }

    fn run_native_input(&mut self, op: String, resource: String, input: Value, keys: Vec<(&'static str, Value)>) {
        let epoch = self.epochs.get(&resource).copied().unwrap_or(0);
        let touch = self.last_touch.get(&resource).copied();
        let focus = json!({"kind": "act", "name": "focus", "input": input});
        self.run_dom_script(
            &resource.clone(),
            focus,
            epoch,
            Box::new(move |app, result| {
                let row = result.unwrap_or(Value::Null);
                if row.get("ok").and_then(Value::as_bool) != Some(true) {
                    let error = row.get("error").and_then(Value::as_str).unwrap_or("focus_failed").to_string();
                    return app.finish_app_control(
                        &op,
                        false,
                        row.get("output").cloned(),
                        &error,
                        get_str(&row, "observation_version"),
                    );
                }
                if !app.still_owned(&resource, touch) {
                    return app.finish_app_control(
                        &op,
                        false,
                        Some(json!({"action_executed": false})),
                        "human_intervention_reinspect_required",
                        "",
                    );
                }
                app.native_input_until.insert(resource.clone(), Instant::now() + Duration::from_secs(1));
                let before = row.get("output").cloned().unwrap_or(json!({}));
                app.cdp_sequence(
                    resource.clone(),
                    keys,
                    Box::new(move |app, sent| {
                        if let Err(error) = sent {
                            return app.finish_app_control(
                                &op,
                                false,
                                Some(json!({"action_executed": false})),
                                &error,
                                "",
                            );
                        }
                        let settle = json!({"kind": "act", "name": "settle", "input": {
                    "before_rev": before.get("rev").cloned().unwrap_or(json!(0)),
                    "before_url": before.get("url").cloned().unwrap_or(json!(""))}});
                        app.run_dom_script(
                            &resource,
                            settle,
                            epoch,
                            Box::new(move |app, result| {
                                let out = result.unwrap_or(Value::Null);
                                let error =
                                    out.get("error").and_then(Value::as_str).map(str::to_string).unwrap_or_else(|| {
                                        if out.is_object() {
                                            String::new()
                                        } else {
                                            "settle_failed".into()
                                        }
                                    });
                                let ok = out.get("ok").and_then(Value::as_bool) == Some(true);
                                app.finish_app_control(
                                    &op,
                                    ok,
                                    out.get("output").cloned(),
                                    &error,
                                    get_str(&out, "observation_version"),
                                );
                            }),
                        );
                    }),
                );
            }),
        );
    }

    fn run_native_pointer(&mut self, op: String, resource: String, name: String, input: Value) {
        let epoch = self.epochs.get(&resource).copied().unwrap_or(0);
        let touch = self.last_touch.get(&resource).copied();
        let mut pointer_input = obj(&input);
        pointer_input.insert("pointer_action".into(), json!(name));
        let prepare = json!({"kind": "act", "name": "prepare_pointer", "input": pointer_input});
        self.run_dom_script(&resource.clone(), prepare, epoch, Box::new(move |app, result| {
            let row = result.unwrap_or(Value::Null);
            let version = get_str(&row, "observation_version").to_string();
            let Some(before) = row.get("output").filter(|_| row.get("ok").and_then(Value::as_bool) == Some(true)).cloned() else {
                let error = row.get("error").and_then(Value::as_str).unwrap_or("pointer_target_failed").to_string();
                return app.finish_app_control(&op, false, row.get("output").cloned(), &error, &version);
            };
            if before.get("skip").and_then(Value::as_bool) == Some(true) {
                return app.finish_app_control(&op, true, Some(before), "", &version);
            }
            let (Some(x), Some(y)) = (before.get("x").and_then(Value::as_f64), before.get("y").and_then(Value::as_f64)) else {
                return app.finish_app_control(&op, false, None, "pointer_coordinates_missing", &version);
            };
            if !app.still_owned(&resource, touch) {
                return app.finish_app_control(&op, false, Some(json!({"action_executed": false})),
                    "human_intervention_reinspect_required", "");
            }
            app.native_input_until.insert(resource.clone(), Instant::now() + Duration::from_secs(1));
            let mut calls = vec![("Input.dispatchMouseEvent", cdp::mouse("mouseMoved", x, y, "none", 0, 0))];
            if name != "hover" {
                let count = if name == "dblclick" { 2 } else { 1 };
                for click in 1..=count {
                    calls.push(("Input.dispatchMouseEvent", cdp::mouse("mousePressed", x, y, "left", 1, click)));
                    calls.push(("Input.dispatchMouseEvent", cdp::mouse("mouseReleased", x, y, "left", 0, click)));
                }
            }
            let hidden = app.browsers.get(&resource).map(|v| v.hidden).unwrap_or(true);
            let diagnostic = json!({"target": before.get("target").cloned().unwrap_or(json!("")),
                "dom": before.get("diagnostic").cloned().unwrap_or(json!({})), "css_x": x, "css_y": y,
                "view_hidden": hidden, "input_path": "cdp"});
            let approval = before.get("approval_revalidated").cloned().unwrap_or(json!(false));
            app.cdp_sequence(resource.clone(), calls, Box::new(move |app, sent| {
                if let Err(error) = sent {
                    return app.finish_app_control(&op, false, Some(json!({"action_executed": false})), &error, "");
                }
                let mut settle_input = obj(&input);
                settle_input.insert("before_rev".into(), before.get("rev").cloned().unwrap_or(json!(0)));
                settle_input.insert("before_url".into(), before.get("url").cloned().unwrap_or(json!("")));
                settle_input.insert("input_path".into(), json!("native_pointer"));
                settle_input.insert("action".into(), json!(name));
                settle_input.insert("diagnostic".into(), diagnostic.clone());
                settle_input.insert("approval_revalidated".into(), approval.clone());
                if name == "check" || name == "uncheck" {
                    settle_input.insert("desired_checked".into(), json!(name == "check"));
                }
                let evidence = json!({"action_executed": true, "delivery": "dispatched", "effect_verified": false,
                    "outcome": "delivery_only", "diagnostic": diagnostic, "approval_revalidated": approval,
                    "requires_reinspect": true, "next_step": "inspect_or_wait_for_postcondition"});
                let settle = json!({"kind": "act", "name": "settle", "input": settle_input});
                app.run_dom_script(&resource, settle, epoch, Box::new(move |app, result| {
                    let out = result.unwrap_or(Value::Null);
                    let mut merged = obj(&evidence);
                    for (key, value) in obj(out.get("output").unwrap_or(&Value::Null)) {
                        merged.insert(key, value);
                    }
                    let error = out.get("error").and_then(Value::as_str).map(str::to_string)
                        .unwrap_or_else(|| if out.is_object() { String::new() } else { "settle_failed".into() });
                    let ok = out.get("ok").and_then(Value::as_bool) == Some(true);
                    app.finish_app_control(&op, ok, Some(Value::Object(merged)), &error, get_str(&out, "observation_version"));
                }));
            }));
        }));
    }

    fn run_native_drag(&mut self, op: String, resource: String, input: Value) {
        let epoch = self.epochs.get(&resource).copied().unwrap_or(0);
        let touch = self.last_touch.get(&resource).copied();
        let prepare = json!({"kind": "act", "name": "prepare_drag", "input": input});
        self.run_dom_script(
            &resource.clone(),
            prepare,
            epoch,
            Box::new(move |app, result| {
                let row = result.unwrap_or(Value::Null);
                let before = row.get("output").cloned().unwrap_or(Value::Null);
                let n = |k: &str| before.get(k).and_then(Value::as_f64);
                let coords = (n("source_x"), n("source_y"), n("target_x"), n("target_y"));
                let (Some(sx), Some(sy), Some(tx), Some(ty)) = coords else {
                    let error = row.get("error").and_then(Value::as_str).unwrap_or("drag_target_failed").to_string();
                    return app.finish_app_control(
                        &op,
                        false,
                        row.get("output").cloned(),
                        &error,
                        get_str(&row, "observation_version"),
                    );
                };
                if row.get("ok").and_then(Value::as_bool) != Some(true) {
                    let error = row.get("error").and_then(Value::as_str).unwrap_or("drag_target_failed").to_string();
                    return app.finish_app_control(
                        &op,
                        false,
                        Some(before),
                        &error,
                        get_str(&row, "observation_version"),
                    );
                }
                if !app.still_owned(&resource, touch) {
                    return app.finish_app_control(
                        &op,
                        false,
                        Some(json!({"action_executed": false})),
                        "human_intervention_reinspect_required",
                        "",
                    );
                }
                app.native_input_until.insert(resource.clone(), Instant::now() + Duration::from_secs(2));
                let mut calls = vec![
                    ("Input.dispatchMouseEvent", cdp::mouse("mouseMoved", sx, sy, "none", 0, 0)),
                    ("Input.dispatchMouseEvent", cdp::mouse("mousePressed", sx, sy, "left", 1, 1)),
                ];
                for step in 1..=10 {
                    let ratio = step as f64 / 10.0;
                    calls.push((
                        "Input.dispatchMouseEvent",
                        cdp::mouse("mouseMoved", sx + (tx - sx) * ratio, sy + (ty - sy) * ratio, "left", 1, 0),
                    ));
                }
                calls.push(("Input.dispatchMouseEvent", cdp::mouse("mouseReleased", tx, ty, "left", 0, 1)));
                app.cdp_sequence(
                    resource.clone(),
                    calls,
                    Box::new(move |app, sent| {
                        if let Err(error) = sent {
                            return app.finish_app_control(
                                &op,
                                false,
                                Some(json!({"action_executed": false})),
                                &error,
                                "",
                            );
                        }
                        let settle = json!({"kind": "act", "name": "settle", "input": {
                    "before_rev": before.get("rev").cloned().unwrap_or(json!(0)),
                    "before_url": before.get("url").cloned().unwrap_or(json!("")),
                    "input_path": "native_drag", "action": "drag"}});
                        app.run_dom_script(
                            &resource,
                            settle,
                            epoch,
                            Box::new(move |app, result| {
                                let out = result.unwrap_or(Value::Null);
                                let error =
                                    out.get("error").and_then(Value::as_str).map(str::to_string).unwrap_or_else(|| {
                                        if out.is_object() {
                                            String::new()
                                        } else {
                                            "settle_failed".into()
                                        }
                                    });
                                let ok = out.get("ok").and_then(Value::as_bool) == Some(true);
                                app.finish_app_control(
                                    &op,
                                    ok,
                                    out.get("output").cloned(),
                                    &error,
                                    get_str(&out, "observation_version"),
                                );
                            }),
                        );
                    }),
                );
            }),
        );
    }

    /// 标签模型归前端所有，激活/关闭回到同一事实源执行。
    fn run_browser_agent_command(&mut self, action: &str, resource: &str, done: impl FnOnce(&mut App, bool) + 'static) {
        let payload = json!({"action": action, "id": resource});
        let js = format!("window.__browserAgentCommand && window.__browserAgentCommand({payload})");
        self.eval_with(None, &js, move |app, result| done(app, matches!(result, Ok(Value::Bool(true)))));
    }

    /// 截图：先在页面遮住密码与敏感字段，再经 DevTools 协议取 PNG。
    fn snapshot(&mut self, op: String, target: String, input: Value, protect_desk: bool, desk: Option<(Value, i64)>) {
        let valid = |app: &App| desk.as_ref().map(|(o, e)| app.desk_operation_valid(o, *e)).unwrap_or(true);
        if !valid(self) {
            return self.finish_app_control(&op, false, None, "human_intervention_reinspect_required", "");
        }
        let protected_rule =
            if protect_desk { "[data-app-control-protected]{visibility:hidden!important}" } else { "" };
        let mask = format!(
            "(() => {{ let style = document.getElementById('__desk_agent_snapshot_mask');\n\
             if (!style) {{ style = document.createElement('style'); style.id = '__desk_agent_snapshot_mask';\n\
             style.textContent = \"input[type=password],input[autocomplete*=password],input[name*=token i],input[name*=secret i],[data-sensitive],[data-private]{{color:transparent!important;text-shadow:0 0 10px #777!important;background:#777!important}}\" + {};\n\
             document.documentElement.appendChild(style); }}\n\
             const root = document.documentElement, body = document.body;\n\
             return {{content_width: Math.max(root?.scrollWidth || 0, body?.scrollWidth || 0, innerWidth),\n\
             content_height: Math.max(root?.scrollHeight || 0, body?.scrollHeight || 0, innerHeight),\n\
             viewport_width: innerWidth, viewport_height: innerHeight}}; }})()",
            crate::app::js_string(protected_rule)
        );
        let unmask = "document.getElementById('__desk_agent_snapshot_mask')?.remove()";
        let browser = if target == MAIN { None } else { Some(target.clone()) };
        self.eval_with(browser.as_deref(), &mask, move |app, metrics| {
            let unmask_now = |app: &mut App, target: &str| {
                if target == MAIN {
                    app.eval_main(unmask);
                } else if let Some(view) = app.browsers.get(target) {
                    let _ = view.webview.evaluate_script(unmask);
                }
            };
            if !desk.as_ref().map(|(o, e)| app.desk_operation_valid(o, *e)).unwrap_or(true) {
                unmask_now(app, &target);
                return app.finish_app_control(&op, false, None, "human_intervention_reinspect_required", "");
            }
            let metrics = metrics.unwrap_or(Value::Null);
            let num = |k: &str, d: f64| metrics.get(k).and_then(Value::as_f64).unwrap_or(d);
            let (vw, vh) = (num("viewport_width", 1280.0), num("viewport_height", 800.0));
            let mut full = input.get("full_page").and_then(Value::as_bool) == Some(true);
            let mut truncated = false;
            let mut params = json!({"format": "png"});
            let (width, height);
            if let Some(clip) = input.get("clip").filter(|c| c.is_object()) {
                let c = |k: &str, d: f64| clip.get(k).and_then(Value::as_f64).unwrap_or(d);
                let (x, y, w, h) = (c("x", -1.0), c("y", -1.0), c("width", 0.0), c("height", 0.0));
                if x < 0.0 || y < 0.0 || w < 1.0 || h < 1.0 || x + w > vw || y + h > vh {
                    unmask_now(app, &target);
                    return app.finish_app_control(&op, false, None, "screenshot_clip_outside_viewport", "");
                }
                params["clip"] = json!({"x": x, "y": y, "width": w, "height": h, "scale": 1});
                full = false;
                width = w;
                height = h;
            } else if full {
                let w = num("content_width", vw).clamp(1.0, 12000.0);
                let mut h = num("content_height", vh).clamp(1.0, 12000.0);
                // 与 Runtime 的 20MB 证据上限同向约束。
                let max_pixels = 20_000_000.0;
                if w * h > max_pixels {
                    h = (max_pixels / w).floor().max(1.0);
                    truncated = true;
                }
                params["clip"] = json!({"x": 0, "y": 0, "width": w, "height": h, "scale": 1});
                params["captureBeyondViewport"] = json!(true);
                width = w;
                height = h;
            } else {
                width = vw;
                height = vh;
            }
            app.cdp(&target.clone(), "Page.captureScreenshot", params, move |app, result| {
                unmask_now(app, &target);
                if !desk.as_ref().map(|(o, e)| app.desk_operation_valid(o, *e)).unwrap_or(true) {
                    return app.finish_app_control(&op, false, None, "human_intervention_reinspect_required", "");
                }
                let data = result.ok().and_then(|v| v.get("data").and_then(Value::as_str).map(str::to_string));
                let Some(data) = data else {
                    return app.finish_app_control(&op, false, None, "snapshot_failed", "");
                };
                let epoch = app.epochs.get(&target).copied().unwrap_or(0);
                let stamp = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_millis())
                    .unwrap_or(0);
                app.finish_app_control(
                    &op,
                    true,
                    Some(json!({"mime_type": "image/png", "data": data,
                    "width": width, "height": height, "full_page": full, "truncated": truncated})),
                    "",
                    &format!("{epoch}:screenshot:{stamp}"),
                );
            });
        });
    }
}
