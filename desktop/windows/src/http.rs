//! 只访问本机 Desk 后端(127.0.0.1)的最小 HTTP/1.1 客户端。
//!
//! 不引入网络库：请求固定发到回环地址，带 `Connection: close`，按 Content-Length
//! 或分块编码读取响应。超时同时作用于连接与读写。

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

pub struct Response {
    pub status: u16,
    pub body: Vec<u8>,
}

pub fn request(
    port: u16,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: Option<&[u8]>,
    timeout: Duration,
) -> Result<Response, String> {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&addr, timeout).map_err(|e| e.to_string())?;
    stream.set_read_timeout(Some(timeout)).ok();
    stream.set_write_timeout(Some(timeout)).ok();
    stream.set_nodelay(true).ok();
    let mut head = format!("{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n");
    for (name, value) in headers {
        head.push_str(name);
        head.push_str(": ");
        head.push_str(value);
        head.push_str("\r\n");
    }
    let payload = body.unwrap_or(&[]);
    if body.is_some() {
        head.push_str(&format!("Content-Length: {}\r\n", payload.len()));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes()).map_err(|e| e.to_string())?;
    if !payload.is_empty() {
        stream.write_all(payload).map_err(|e| e.to_string())?;
    }
    let mut reader = BufReader::new(stream);
    let mut status_line = String::new();
    reader.read_line(&mut status_line).map_err(|e| e.to_string())?;
    let status = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| "无效的 HTTP 响应".to_string())?;
    let mut length: Option<usize> = None;
    let mut chunked = false;
    loop {
        let mut line = String::new();
        let read = reader.read_line(&mut line).map_err(|e| e.to_string())?;
        if read == 0 || line == "\r\n" || line == "\n" {
            break;
        }
        let lower = line.to_ascii_lowercase();
        if let Some(value) = lower.strip_prefix("content-length:") {
            length = value.trim().parse().ok();
        } else if lower.starts_with("transfer-encoding:") && lower.contains("chunked") {
            chunked = true;
        }
    }
    let mut body = Vec::new();
    if chunked {
        loop {
            let mut size_line = String::new();
            reader.read_line(&mut size_line).map_err(|e| e.to_string())?;
            let size = usize::from_str_radix(size_line.trim().split(';').next().unwrap_or("0"), 16)
                .map_err(|_| "无效的分块长度".to_string())?;
            if size == 0 {
                break;
            }
            let mut chunk = vec![0u8; size];
            reader.read_exact(&mut chunk).map_err(|e| e.to_string())?;
            body.extend_from_slice(&chunk);
            let mut crlf = [0u8; 2];
            reader.read_exact(&mut crlf).map_err(|e| e.to_string())?;
        }
    } else if let Some(length) = length {
        body.resize(length, 0);
        reader.read_exact(&mut body).map_err(|e| e.to_string())?;
    } else {
        reader.read_to_end(&mut body).map_err(|e| e.to_string())?;
    }
    Ok(Response { status, body })
}

/// JSON 请求；返回 (解析后的对象, 错误说明)。语义与 macOS 壳的 appControlRequest 一致：
/// 2xx 以外返回 `error` 字段或 `http_<状态码>`。
pub fn json(
    port: u16,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: Option<&serde_json::Value>,
    timeout: Duration,
) -> (Option<serde_json::Value>, Option<String>) {
    let encoded = body.map(|value| serde_json::to_vec(value).unwrap_or_default());
    let mut all: Vec<(&str, &str)> = headers.to_vec();
    if encoded.is_some() {
        all.push(("Content-Type", "application/json"));
    }
    match request(port, method, path, &all, encoded.as_deref(), timeout) {
        Err(error) => (None, Some(error)),
        Ok(response) => {
            let value: Option<serde_json::Value> = serde_json::from_slice(&response.body).ok();
            if (200..300).contains(&response.status) {
                (value, None)
            } else {
                let message = value
                    .as_ref()
                    .and_then(|v| v.get("error"))
                    .and_then(|v| v.as_str())
                    .map(str::to_string)
                    .unwrap_or_else(|| format!("http_{}", response.status));
                (value, Some(message))
            }
        }
    }
}
