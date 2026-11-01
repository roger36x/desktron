//! Desktron Windows 原生壳。
//!
//! 职责与 macOS Swift 壳相同：定位产品目录、以唯一 Supervisor 身份拉起并看护 Python 后端、
//! 用 WebView2 承载工作台与内置浏览器、执行 App Control 宿主侧动作、处理窗口与进程生命周期。
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

#[cfg(windows)]
mod app;
#[cfg(windows)]
mod backdrop;
#[cfg(windows)]
mod cdp;
#[cfg(windows)]
mod chrome;
#[cfg(windows)]
mod control;
#[cfg(windows)]
mod dialogs;
#[cfg(windows)]
mod http;
#[cfg(windows)]
mod network;
#[cfg(windows)]
mod paths;
#[cfg(windows)]
mod supervisor;
#[cfg(windows)]
mod views;
#[cfg(windows)]
mod winutil;

#[cfg(windows)]
fn main() {
    app::run();
}

#[cfg(not(windows))]
fn main() {
    eprintln!("Desktron Windows 原生壳只能在 Windows 上运行；macOS 使用 desktop/native 下的 Swift 壳。");
    std::process::exit(1);
}
