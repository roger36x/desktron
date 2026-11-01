//! 系统对话框。全部在后台线程打开(模态于主窗口)，结果经事件回到主线程，
//! 不在事件循环回调里嵌套模态循环。

use std::num::NonZeroIsize;
use std::path::PathBuf;
use std::thread;

use raw_window_handle::{
    DisplayHandle, HandleError, HasDisplayHandle, HasWindowHandle, RawWindowHandle, Win32WindowHandle, WindowHandle,
    WindowsDisplayHandle,
};
use rfd::{FileDialog, MessageButtons, MessageDialog, MessageDialogResult, MessageLevel};
use tao::event_loop::EventLoopProxy;

use crate::app::UserEvent;

pub enum Answer {
    Confirmed(bool),
    Path(Option<PathBuf>),
}

/// 以原始 HWND 作为对话框父窗口；对话框线程不持有 tao 窗口对象。
struct Parent(isize);

impl HasWindowHandle for Parent {
    fn window_handle(&self) -> Result<WindowHandle<'_>, HandleError> {
        let hwnd = NonZeroIsize::new(self.0).ok_or(HandleError::Unavailable)?;
        let raw = RawWindowHandle::Win32(Win32WindowHandle::new(hwnd));
        Ok(unsafe { WindowHandle::borrow_raw(raw) })
    }
}

impl HasDisplayHandle for Parent {
    fn display_handle(&self) -> Result<DisplayHandle<'_>, HandleError> {
        Ok(unsafe { DisplayHandle::borrow_raw(WindowsDisplayHandle::new().into()) })
    }
}

pub fn alert_blocking(title: &str, text: &str) {
    MessageDialog::new()
        .set_level(MessageLevel::Info)
        .set_title(title)
        .set_description(text)
        .set_buttons(MessageButtons::Ok)
        .show();
}

pub fn confirm(
    proxy: EventLoopProxy<UserEvent>,
    token: u64,
    parent: isize,
    title: String,
    detail: String,
    accept: String,
    reject: String,
) {
    thread::spawn(move || {
        let result = MessageDialog::new()
            .set_level(MessageLevel::Warning)
            .set_title(&title)
            .set_description(&detail)
            .set_parent(&Parent(parent))
            .set_buttons(MessageButtons::OkCancelCustom(accept.clone(), reject))
            .show();
        let accepted = matches!(&result, MessageDialogResult::Custom(label) if *label == accept)
            || matches!(result, MessageDialogResult::Ok | MessageDialogResult::Yes);
        let _ = proxy.send_event(UserEvent::Dialog { token, result: Answer::Confirmed(accepted) });
    });
}

pub fn pick_folder(proxy: EventLoopProxy<UserEvent>, token: u64, parent: isize) {
    thread::spawn(move || {
        let path =
            FileDialog::new().set_title("选择一个目录作为 coding 项目").set_parent(&Parent(parent)).pick_folder();
        let _ = proxy.send_event(UserEvent::Dialog { token, result: Answer::Path(path) });
    });
}

/// 下载保存位置。下载回调是同步的，这里在调用线程上阻塞显示(系统对话框自带消息循环)。
pub fn save_file_blocking(parent: isize, suggested: &str) -> Option<PathBuf> {
    let mut dialog = FileDialog::new().set_title("选择下载文件的保存位置").set_parent(&Parent(parent));
    if !suggested.is_empty() {
        dialog = dialog.set_file_name(suggested);
    }
    if let Some(downloads) = std::env::var_os("USERPROFILE").map(|h| PathBuf::from(h).join("Downloads")) {
        dialog = dialog.set_directory(downloads);
    }
    dialog.save_file()
}
