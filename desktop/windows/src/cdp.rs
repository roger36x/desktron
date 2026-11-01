//! WebView2 DevTools 协议调用。
//!
//! macOS 壳用 WKContentWorld 在隔离世界执行 App Control 脚本、用 NSEvent 送真实输入、用
//! takeSnapshot 截图。WebView2 没有对应的公开接口，这里用 DevTools 协议实现同样语义：
//! - `Page.createIsolatedWorld` + `Runtime.evaluate(contextId)`：页面脚本看不到、改不了；
//! - `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent`：页面收到 isTrusted=true 的事件；
//! - `Page.captureScreenshot`：PNG 截图，支持裁剪与整页。
//!
//! 所有调用异步完成，结果经 EventLoopProxy 回到主线程。

use serde_json::{json, Value};
use tao::event_loop::EventLoopProxy;
use webview2_com::CallDevToolsProtocolMethodCompletedHandler;
use webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2;
use windows::core::HSTRING;

use crate::app::UserEvent;

pub const WORLD_NAME: &str = "desk-agent-control";

/// 发起一次协议调用；完成后发送 `UserEvent::Cdp { token, result }`。
pub fn call(webview: &ICoreWebView2, proxy: &EventLoopProxy<UserEvent>, token: u64, method: &str, params: Value) {
    let fallback = proxy.clone();
    let proxy = proxy.clone();
    let handler = CallDevToolsProtocolMethodCompletedHandler::create(Box::new(move |result, json| {
        let outcome = match result {
            Ok(()) => serde_json::from_str::<Value>(&json).map_err(|e| e.to_string()),
            Err(error) => Err(error.message().to_string()),
        };
        let _ = proxy.send_event(UserEvent::Cdp { token, result: outcome });
        Ok(())
    }));
    let method = HSTRING::from(method);
    let params = HSTRING::from(params.to_string());
    let outcome = unsafe { webview.CallDevToolsProtocolMethod(&method, &params, &handler) };
    if let Err(error) = outcome {
        let _ = fallback.send_event(UserEvent::Cdp { token, result: Err(error.message().to_string()) });
    }
}

/// `callAsyncJavaScript(body, arguments)` 的等价表达式：把函数体包成带具名参数的异步函数。
pub fn async_function_expression(body: &str, names: &[&str], values: &[Value]) -> String {
    let args = values.iter().map(|v| v.to_string()).collect::<Vec<_>>().join(",");
    format!("(async ({}) => {{\n{}\n}})({})", names.join(","), body, args)
}

/// Runtime.evaluate 的返回值：异常时取异常说明。
pub fn evaluate_result(value: &Value) -> Result<Value, String> {
    if let Some(details) = value.get("exceptionDetails") {
        let text = details
            .get("exception")
            .and_then(|e| e.get("description"))
            .and_then(Value::as_str)
            .or_else(|| details.get("text").and_then(Value::as_str))
            .unwrap_or("script_exception");
        return Err(text.to_string());
    }
    Ok(value.get("result").and_then(|r| r.get("value")).cloned().unwrap_or(Value::Null))
}

pub fn evaluate_params(expression: &str, context_id: i64) -> Value {
    json!({"expression": expression, "contextId": context_id, "awaitPromise": true,
           "returnByValue": true, "userGesture": false})
}

pub fn create_world_params(frame_id: &str) -> Value {
    json!({"frameId": frame_id, "worldName": WORLD_NAME, "grantUniveralAccess": false})
}

/// CDP 键盘事件：(key, code, windowsVirtualKeyCode, text)。参照常见美式布局。
pub fn special_key(name: &str) -> Option<(&'static str, &'static str, u32, &'static str)> {
    Some(match name.to_ascii_lowercase().as_str() {
        "enter" | "return" => ("Enter", "Enter", 13, "\r"),
        "tab" => ("Tab", "Tab", 9, ""),
        "escape" | "esc" => ("Escape", "Escape", 27, ""),
        "space" | " " => (" ", "Space", 32, " "),
        "backspace" => ("Backspace", "Backspace", 8, ""),
        "delete" => ("Delete", "Delete", 46, ""),
        "arrowleft" => ("ArrowLeft", "ArrowLeft", 37, ""),
        "arrowright" => ("ArrowRight", "ArrowRight", 39, ""),
        "arrowdown" => ("ArrowDown", "ArrowDown", 40, ""),
        "arrowup" => ("ArrowUp", "ArrowUp", 38, ""),
        "home" => ("Home", "Home", 36, ""),
        "end" => ("End", "End", 35, ""),
        "pageup" => ("PageUp", "PageUp", 33, ""),
        "pagedown" => ("PageDown", "PageDown", 34, ""),
        _ => return None,
    })
}

/// 把 press 的 key / type 的 text 翻成一串 CDP 调用(方法, 参数)。映射不了返回 None，
/// 由页面内合成事件兜底——与 macOS 壳的 nativeKeyEvents 规则一致。
pub fn key_events(name: &str, input: &Value) -> Option<Vec<(&'static str, Value)>> {
    let mut out = Vec::new();
    if name == "type" {
        let text = input.get("text").and_then(Value::as_str).unwrap_or("");
        if text.is_empty() {
            return None;
        }
        for ch in text.chars() {
            if ch == '\n' {
                push_key(&mut out, "Enter", "Enter", 13, "\r", 0);
            } else if ch.is_ascii() && !ch.is_ascii_control() {
                let s = ch.to_string();
                let code = ascii_code(ch);
                let vk = ascii_vk(ch);
                push_key_owned(&mut out, s.clone(), code, vk, s, 0);
            } else {
                // 非 ASCII(中文等)以输入法提交的方式送达，页面收到 input 事件。
                out.push(("Input.insertText", json!({"text": ch.to_string()})));
            }
        }
        return Some(out);
    }
    let raw = input.get("key").and_then(Value::as_str).unwrap_or("");
    let mut parts: Vec<&str> = raw.split('+').collect();
    let last = parts.pop().filter(|s| !s.is_empty())?;
    let mut modifiers = 0u32;
    for part in parts {
        modifiers |= match part.to_ascii_lowercase().as_str() {
            "alt" | "option" => 1,
            "control" | "ctrl" => 2,
            "meta" | "cmd" | "command" => 4,
            "shift" => 8,
            _ => return None,
        };
    }
    if let Some((key, code, vk, text)) = special_key(last) {
        // 组合键不产生文本输入。
        let text = if modifiers & (1 | 2 | 4) != 0 { "" } else { text };
        push_key(&mut out, key, code, vk, text, modifiers);
        return Some(out);
    }
    let mut chars = last.chars();
    if let (Some(ch), None) = (chars.next(), chars.next()) {
        if !ch.is_ascii() {
            return None;
        }
        let text = if modifiers & (1 | 2 | 4) != 0 { String::new() } else { ch.to_string() };
        push_key_owned(&mut out, ch.to_string(), ascii_code(ch), ascii_vk(ch), text, modifiers);
        return Some(out);
    }
    None
}

fn push_key(out: &mut Vec<(&'static str, Value)>, key: &str, code: &str, vk: u32, text: &str, modifiers: u32) {
    push_key_owned(out, key.to_string(), code.to_string(), vk, text.to_string(), modifiers);
}

fn push_key_owned(
    out: &mut Vec<(&'static str, Value)>,
    key: String,
    code: String,
    vk: u32,
    text: String,
    modifiers: u32,
) {
    let mut down = json!({"type": if text.is_empty() { "rawKeyDown" } else { "keyDown" },
        "key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
        "modifiers": modifiers});
    if !text.is_empty() {
        down["text"] = Value::String(text.clone());
        down["unmodifiedText"] = Value::String(text);
    }
    out.push(("Input.dispatchKeyEvent", down));
    out.push((
        "Input.dispatchKeyEvent",
        json!({"type": "keyUp", "key": key, "code": code,
        "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": modifiers}),
    ));
}

fn ascii_code(ch: char) -> String {
    match ch {
        'a'..='z' => format!("Key{}", ch.to_ascii_uppercase()),
        'A'..='Z' => format!("Key{ch}"),
        '0'..='9' => format!("Digit{ch}"),
        ' ' => "Space".into(),
        _ => String::new(),
    }
}

fn ascii_vk(ch: char) -> u32 {
    match ch {
        'a'..='z' => ch.to_ascii_uppercase() as u32,
        'A'..='Z' | '0'..='9' | ' ' => ch as u32,
        _ => 0,
    }
}

pub fn mouse(kind: &str, x: f64, y: f64, button: &str, buttons: u32, clicks: u32) -> Value {
    json!({"type": kind, "x": x, "y": y, "button": button, "buttons": buttons, "clickCount": clicks})
}
