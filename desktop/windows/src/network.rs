//! One launch-time policy for every WebView2. Python owns validation and OS proxy resolution.
use std::os::windows::process::CommandExt;
use std::process::Command;

use serde::Deserialize;

use crate::paths;

#[derive(Deserialize)]
struct Policy {
    args: Vec<String>,
    revision: String,
    resolution: String,
}

pub fn browser_args(base: &str) -> Result<String, String> {
    let root = paths::repo_root();
    let python = paths::python(&root)?;
    let output = Command::new(python)
        .arg(root.join("desktron/lib/network_proxy.py"))
        .arg("--native")
        .env("PYTHONIOENCODING", "utf-8")
        .creation_flags(0x08000000) // CREATE_NO_WINDOW; never flash a console at startup.
        .output()
        .map_err(|_| "无法读取网络代理设置；未回退直连。".to_string())?;
    if !output.status.success() {
        return Err("网络代理配置损坏或不受支持。请检查本机 network.json；未回退直连。".into());
    }
    let policy: Policy =
        serde_json::from_slice(&output.stdout).map_err(|_| "网络代理配置协议错误；未回退直连。".to_string())?;
    std::env::set_var("DESKTRON_BROWSER_NETWORK_REVISION", policy.revision);
    std::env::set_var("DESKTRON_BROWSER_NETWORK_RESOLUTION", policy.resolution);
    let mut args = base.to_string();
    for arg in policy.args {
        args.push(' ');
        args.push_str(&arg);
    }
    Ok(args)
}
