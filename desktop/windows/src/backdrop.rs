//! Windows 11 Desktop Acrylic：原生窗口材质与透明 WebView2 分开配置。
//! 不使用未公开的 SetWindowCompositionAttribute；旧系统/API 失败必须显式记录并用纯色底。

use tao::platform::windows::WindowExtWindows;
use tao::window::Window;
use windows::Win32::Foundation::HWND;
use windows::Win32::Graphics::Dwm::{
    DwmExtendFrameIntoClientArea, DwmSetWindowAttribute, DWMSBT_NONE, DWMSBT_TRANSIENTWINDOW,
    DWMWA_SYSTEMBACKDROP_TYPE, DWMWA_USE_IMMERSIVE_DARK_MODE,
};
use windows::Win32::UI::Controls::MARGINS;

pub fn apply(window: &Window, light: bool) -> Result<(), String> {
    let hwnd = HWND(window.hwnd() as *mut _);
    unsafe {
        let dark: i32 = i32::from(!light);
        DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            &dark as *const _ as _, std::mem::size_of_val(&dark) as u32)
            .map_err(|e| format!("window_backdrop_theme_failed: {e}"))?;
        let material = DWMSBT_TRANSIENTWINDOW; // 系统 Desktop Acrylic，而非仅取壁纸颜色的 Mica。
        DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
            &material as *const _ as _, std::mem::size_of_val(&material) as u32)
            .map_err(|e| format!("window_backdrop_acrylic_failed: {e}"))?;
        let margins = MARGINS { cxLeftWidth: -1, cxRightWidth: -1, cyTopHeight: -1, cyBottomHeight: -1 };
        DwmExtendFrameIntoClientArea(hwnd, &margins)
            .map_err(|e| format!("window_backdrop_frame_failed: {e}"))?;
    }
    // 系统负责失焦、关闭透明效果、节电等情况下的材质降级；不修改用户系统设置。
    Ok(())
}

pub fn disable(window: &Window) -> Result<(), String> {
    let hwnd = HWND(window.hwnd() as *mut _);
    unsafe {
        let material = DWMSBT_NONE;
        // 旧系统也可能不支持 NONE；仍需把 frame 扩展复位，不能提前返回。
        let material_result = DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
            &material as *const _ as _, std::mem::size_of_val(&material) as u32);
        let frame_result = DwmExtendFrameIntoClientArea(hwnd, &MARGINS::default());
        material_result.and(frame_result).map_err(|e| format!("window_backdrop_reset_failed: {e}"))
    }
}

pub fn background(acrylic: bool, light: bool) -> (u8, u8, u8, u8) {
    // WebView2 只支持全透明或不透明，不传部分 alpha。
    if acrylic { (0, 0, 0, 0) } else if light { (246, 246, 246, 255) } else { (26, 26, 26, 255) }
}

// 新文档先用安全的纯色 CSS，随后由 MainContentLoading 投影当前状态；不冻结启动时的能力。
pub const PENDING_SCRIPT: &str = "window.__deskWindowsBackdrop='pending';";

pub fn script(acrylic: bool) -> &'static str {
    if acrylic {
        "(()=>{window.__deskWindowsBackdrop='acrylic';const mark=()=>document.documentElement.classList.remove('no-vibrancy');if(document.documentElement)mark();else document.addEventListener('DOMContentLoaded',mark,{once:true});})();"
    } else {
        "(()=>{window.__deskWindowsBackdrop='solid';const mark=()=>document.documentElement.classList.add('no-vibrancy');if(document.documentElement)mark();else document.addEventListener('DOMContentLoaded',mark,{once:true});})();"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn webview_is_fully_transparent_only_when_native_material_is_available() {
        for light in [false, true] { assert_eq!(background(true, light), (0, 0, 0, 0)); }
        assert_eq!(background(false, false), (26, 26, 26, 255));
        assert_eq!(background(false, true), (246, 246, 246, 255));
    }

    // 显式桌面验收：仅合成页面、独立 WebView2 profile；不启动后端、不读真实会话。
    #[test]
    #[ignore = "desktop: creates an isolated native window"]
    fn isolated_acrylic_webview_theme_and_layout() {
        use std::time::{Duration, Instant};
        use tao::dpi::LogicalSize;
        use tao::event_loop::EventLoopBuilder;
        use tao::platform::windows::{EventLoopBuilderExtWindows, WindowBuilderExtWindows};
        use tao::window::WindowBuilder;
        use windows::core::Interface;
        use windows::Win32::Graphics::Dwm::DwmGetWindowAttribute;
        use windows::Win32::UI::WindowsAndMessaging::{DispatchMessageW, PeekMessageW, TranslateMessage, MSG, PM_REMOVE};
        use wry::{WebContext, WebViewBuilder, WebViewBuilderExtWindows, WebViewExtWindows};
        let event_loop = EventLoopBuilder::<()>::new().with_any_thread(true).build();
        let window = WindowBuilder::new().with_title("Desk isolated Acrylic check")
            .with_decorations(false).with_transparent(true).with_undecorated_shadow(true)
            .with_inner_size(LogicalSize::new(900.0, 600.0))
            .with_min_inner_size(LogicalSize::new(900.0, 600.0)).build(&event_loop).unwrap();
        let profile = std::env::temp_dir().join(format!("desk-backdrop-{}", uuid::Uuid::new_v4()));
        let mut context = WebContext::new(Some(profile.clone()));
        let (tx, rx) = std::sync::mpsc::channel();
        let web = WebViewBuilder::new_with_web_context(&mut context)
            .with_background_color(background(true, false))
            .with_additional_browser_args("--disable-background-networking")
            .with_ipc_handler(move |request| { let _ = tx.send(request.into_body()); })
            .with_html("<!doctype html><html><body style='margin:0;background:transparent;color:#ccc'><aside style='position:absolute;inset:0 auto 0 0;width:220px;padding-top:64px;box-sizing:border-box;background:rgba(18,18,19,.5);backdrop-filter:blur(18px) saturate(.96) brightness(.97)'>Synthetic sidebar</aside><main id='main' style='margin-left:220px;background:rgb(26,26,26);height:100vh'>Synthetic opaque main</main></body></html>")
            .build_as_child(&window).unwrap();
        let controller: webview2_com::Microsoft::Web::WebView2::Win32::ICoreWebView2Controller2 = web.controller().cast().unwrap();
        let chrome = crate::chrome::Chrome::new(&window, &web).unwrap();
        chrome.set_left(220.0).unwrap();
        chrome.set_acrylic(true);
        let hwnd = HWND(window.hwnd() as *mut _);
        let mut scenarios = vec![
            (false, 900.0, false, 220.0),
            (false, 900.0, true, 220.0),
            (false, 900.0, false, 220.0),
            (true, 1100.0, false, 220.0),
            (false, 900.0, false, 0.0),
            (false, 900.0, false, 220.0),
        ];
        for _ in 0..10 {
            scenarios.extend([(false, 900.0, true, 220.0), (false, 900.0, false, 220.0)]);
        }
        for (step, (light, width, maximized, left)) in scenarios.into_iter().enumerate() {
            unsafe {
                use windows::Win32::UI::WindowsAndMessaging::*;
                // 几何/任务栏验收不使用 TOPMOST。仅在后面的像素截图阶段临时置顶。
                SetWindowPos(hwnd, Some(HWND_NOTOPMOST), 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE).unwrap();
            }
            window.set_theme(Some(if light { tao::window::Theme::Light } else { tao::window::Theme::Dark }));
            apply(&window, light).unwrap();
            let mut material: i32 = 0;
            unsafe { DwmGetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, &mut material as *mut _ as _, 4).unwrap(); }
            assert_eq!(material, DWMSBT_TRANSIENTWINDOW.0);
            web.set_background_color(background(true, light)).unwrap();
            let mut color = Default::default();
            unsafe { controller.DefaultBackgroundColor(&mut color).unwrap(); }
            assert_eq!(color.A, 0);
            if step % 2 == 0 {
                window.set_maximized(maximized);
            } else {
                unsafe {
                    use windows::Win32::Foundation::WPARAM;
                    use windows::Win32::UI::WindowsAndMessaging::*;
                    // 生产按钮走的系统命令链，而非只测试 tao API。
                    SendMessageW(hwnd, WM_SYSCOMMAND,
                        Some(WPARAM(if maximized { SC_MAXIMIZE } else { SC_RESTORE } as usize)), None);
                }
            }
            if !maximized {
                window.set_inner_size(LogicalSize::new(width, 600.0));
                window.set_outer_position(tao::dpi::LogicalPosition::new(80.0 + step as f64 * 10.0, 80.0));
            }
            unsafe { let mut msg = MSG::default(); while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() { let _ = TranslateMessage(&msg); DispatchMessageW(&msg); } }
            let size = window.inner_size().to_logical::<f64>(window.scale_factor());
            web.set_bounds(wry::Rect { position: tao::dpi::LogicalPosition::new(0.0, 0.0).into(), size: size.into() }).unwrap();
            chrome.set_light(light);
            chrome.set_left(left).unwrap();
            unsafe {
                use windows::Win32::UI::WindowsAndMessaging::*;
                let style = GetWindowLongPtrW(hwnd, GWL_STYLE) as u32;
                assert_eq!(style & WS_CAPTION.0, 0, "DWM must not draw a second caption");
                for flag in [WS_THICKFRAME, WS_SYSMENU, WS_MINIMIZEBOX, WS_MAXIMIZEBOX] {
                    assert_ne!(style & flag.0, 0, "native window capability lost: {flag:?}");
                }
            }
            // 等待合成页面加载后读取 DOM；不截取桌面或其他应用。
            let deadline = Instant::now() + Duration::from_secs(10);
            let mut received = None;
            while Instant::now() < deadline {
                web.evaluate_script(&format!("if(document.getElementById('main')){{document.getElementById('main').style.backgroundColor='{}';document.querySelector('aside').style.background='{}';window.ipc.postMessage(JSON.stringify({{step:{step},body:getComputedStyle(document.body).backgroundColor,main:getComputedStyle(document.getElementById('main')).backgroundColor}}));}}",
                    if light { "rgb(246,246,246)" } else { "rgb(26,26,26)" },
                    if light { "rgba(248,248,249,.5)" } else { "rgba(18,18,19,.5)" })).unwrap();
                unsafe { let mut msg = MSG::default(); while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() { let _ = TranslateMessage(&msg); DispatchMessageW(&msg); } }
                while let Ok(value) = rx.try_recv() {
                    let result: serde_json::Value = serde_json::from_str(&value).unwrap();
                    if result["step"] == step { received = Some(value); break; }
                }
                if received.is_some() { break; }
                std::thread::sleep(Duration::from_millis(20));
            }
            let result: serde_json::Value = serde_json::from_str(&received.expect("synthetic WebView did not render")).unwrap();
            assert_eq!(result["body"], "rgba(0, 0, 0, 0)");
            assert_eq!(result["main"], if light { "rgb(246, 246, 246)" } else { "rgb(26, 26, 26)" });
            // 只截取本测试创建的合成窗口，不读桌面或现有 Desk 窗口。
            for _ in 0..40 {
                unsafe { let mut msg = MSG::default(); while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() { let _ = TranslateMessage(&msg); DispatchMessageW(&msg); } }
                std::thread::sleep(Duration::from_millis(20));
            }
            assert_window_geometry(hwnd, maximized);
            unsafe {
                use windows::Win32::UI::WindowsAndMessaging::*;
                SetWindowPos(hwnd, Some(HWND_TOPMOST), 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE).unwrap();
            }
            for _ in 0..10 {
                unsafe { let mut msg = MSG::default(); while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() { let _ = TranslateMessage(&msg); DispatchMessageW(&msg); } }
                std::thread::sleep(Duration::from_millis(20));
            }
            if !maximized { assert_resize_routing(hwnd); }
            if step == 0 {
                unsafe {
                    use windows::Win32::UI::WindowsAndMessaging::*;
                    let mut r = windows::Win32::Foundation::RECT::default();
                    GetClientRect(hwnd, &mut r).unwrap();
                    // 模拟生产browser/tag创建并置顶一个新child，随后恢复边缘输入叠层。
                    let sibling = CreateWindowExW(WINDOW_EX_STYLE::default(), windows::core::w!("STATIC"), windows::core::w!(""),
                        WS_CHILD | WS_VISIBLE, r.right - 30, r.bottom / 2 - 15, 30, 30, Some(hwnd), None, None, None).unwrap();
                    SetWindowPos(sibling, Some(HWND_TOP), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE).unwrap();
                    chrome.raise_resize().unwrap();
                    assert_resize_routing(hwnd);
                    DestroyWindow(sibling).unwrap();
                }
            }
            snapshot(hwnd, &format!("{step}-{}-{width}", if light { "light" } else { "dark" }), window.scale_factor(), true);
        }
        disable(&window).unwrap();
        chrome.set_acrylic(false);
        web.set_background_color(background(false, true)).unwrap();
        let mut color = Default::default();
        unsafe { controller.DefaultBackgroundColor(&mut color).unwrap(); }
        assert_eq!(color.A, 255);
        // 纯色回退与恢复后仍使用相同的生产标题栏，不重新创建窗口。
        for acrylic in [false, true] {
            if acrylic { apply(&window, false).unwrap(); }
            web.set_background_color(background(acrylic, false)).unwrap();
            web.evaluate_script("document.getElementById('main').style.backgroundColor='rgb(26,26,26)';document.querySelector('aside').style.background='rgba(18,18,19,.5)';").unwrap();
            chrome.set_light(false);
            chrome.set_acrylic(acrylic);
            for _ in 0..15 {
                unsafe { let mut msg = MSG::default(); while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() { let _ = TranslateMessage(&msg); DispatchMessageW(&msg); } }
                std::thread::sleep(Duration::from_millis(20));
            }
            snapshot(hwnd, if acrylic { "restored" } else { "fallback" }, window.scale_factor(), acrylic);
        }
        // 覆盖顶层先销毁、Chrome后释放的顺序，防止过期HWND在Drop中被复用。
        unsafe { windows::Win32::UI::WindowsAndMessaging::DestroyWindow(hwnd).unwrap(); }
        assert!(chrome.owned_windows_destroyed());
        drop(controller); drop(chrome); drop(web); drop(context); drop(window); drop(event_loop);
        // WebView2 子进程可能短暂持有缓存，不强杀。测试目录只有合成数据。
        let _ = std::fs::remove_dir_all(profile);
    }

    // 必须检查 outer rect，而不只是 tao 已经裁到 rcWork 的 client rect。
    fn assert_window_geometry(hwnd: HWND, maximized: bool) {
        use windows::Win32::Foundation::{LPARAM, POINT, RECT, WPARAM};
        use windows::Win32::Graphics::Gdi::*;
        use windows::Win32::UI::WindowsAndMessaging::*;
        unsafe {
            let mut outer = RECT::default();
            let mut client = RECT::default();
            let mut origin = POINT::default();
            GetWindowRect(hwnd, &mut outer).unwrap();
            GetClientRect(hwnd, &mut client).unwrap();
            assert!(ClientToScreen(hwnd, &mut origin).as_bool());
            assert_eq!(IsZoomed(hwnd).as_bool(), maximized, "window state disagrees with scenario");
            assert_eq!(GetWindowLongPtrW(hwnd, GWL_EXSTYLE) as u32 & WS_EX_TOPMOST.0, 0,
                "work-area assertion must run on a normal non-topmost window");
            let mut limits = MINMAXINFO::default();
            SendMessageW(hwnd, WM_GETMINMAXINFO, Some(WPARAM(0)), Some(LPARAM(&mut limits as *mut _ as isize)));
            let dpi = windows::Win32::UI::HiDpi::GetDpiForWindow(hwnd).max(96) as f64;
            assert!(limits.ptMinTrackSize.x as f64 >= 900.0 * dpi / 96.0
                && limits.ptMinTrackSize.y as f64 >= 600.0 * dpi / 96.0, "production minimum size constraint lost");
            let mut visible = RECT::default();
            windows::Win32::Graphics::Dwm::DwmGetWindowAttribute(hwnd,
                windows::Win32::Graphics::Dwm::DWMWA_EXTENDED_FRAME_BOUNDS,
                &mut visible as *mut _ as _, std::mem::size_of_val(&visible) as u32).unwrap();
            if maximized {
                assert_eq!((origin.x, origin.y), (outer.left, outer.top), "unexpected maximized NC inset");
                assert_eq!((client.right, client.bottom),
                    (outer.right - outer.left, outer.bottom - outer.top), "maximized NC border occupies layout");
                let mut monitor = MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
                assert!(GetMonitorInfoW(MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), &mut monitor).as_bool());
                let work = monitor.rcWork;
                assert!(outer.left >= work.left && outer.top >= work.top && outer.right <= work.right && outer.bottom <= work.bottom,
                    "maximized OUTER rectangle covers taskbar: {outer:?}, work={work:?}");
                assert!(visible.left >= work.left && visible.top >= work.top && visible.right <= work.right && visible.bottom <= work.bottom,
                    "DWM visible frame covers taskbar: {visible:?}, work={work:?}");
                assert!((outer.left - work.left).abs() <= 1 && (outer.top - work.top).abs() <= 1
                    && (outer.right - work.right).abs() <= 1 && (outer.bottom - work.bottom).abs() <= 1);
            } else {
                assert_eq!((origin.x, origin.y), (outer.left, outer.top), "normal window retains painted NC inset");
                assert_eq!((client.right, client.bottom),
                    (outer.right - outer.left, outer.bottom - outer.top), "normal window retains NC border");
                let middle_x = (outer.left + outer.right) / 2;
                let middle_y = (outer.top + outer.bottom) / 2;
                for (x, y, hit) in [
                    (outer.left + 2, middle_y, HTLEFT), (outer.right - 2, middle_y, HTRIGHT),
                    (middle_x, outer.top + 2, HTTOP), (middle_x, outer.bottom - 2, HTBOTTOM),
                    (outer.left + 2, outer.top + 2, HTTOPLEFT), (outer.right - 2, outer.top + 2, HTTOPRIGHT),
                    (outer.left + 2, outer.bottom - 2, HTBOTTOMLEFT), (outer.right - 2, outer.bottom - 2, HTBOTTOMRIGHT),
                ] {
                    let lp = LPARAM(((x as u16 as u32) | ((y as u16 as u32) << 16)) as isize);
                    assert_eq!(SendMessageW(hwnd, WM_NCHITTEST, Some(WPARAM(0)), Some(lp)).0 as u32, hit, "resize hit lost at ({x},{y})");
                }
            }
        }
    }

    // 仅复制自建 TOPMOST HWND 的客户区呈现，不截取整桌面或遍历其他窗口。
    fn assert_resize_routing(hwnd: windows::Win32::Foundation::HWND) {
        use windows::Win32::Foundation::{POINT, RECT, WPARAM, LPARAM};
        use windows::Win32::UI::WindowsAndMessaging::*;
        unsafe {
            let mut r = RECT::default(); GetWindowRect(hwnd, &mut r).unwrap();
            let x = (r.left + r.right) / 2; let y = (r.top + r.bottom) / 2;
            for (x, y, want) in [(r.left+1,y,HTLEFT),(r.right-2,y,HTRIGHT),(x,r.top+1,HTTOP),(x,r.bottom-2,HTBOTTOM),
                (r.left+1,r.top+1,HTTOPLEFT),(r.right-2,r.top+1,HTTOPRIGHT),
                (r.left+1,r.bottom-2,HTBOTTOMLEFT),(r.right-2,r.bottom-2,HTBOTTOMRIGHT)] {
                let target = WindowFromPoint(POINT { x, y });
                assert_eq!(GetAncestor(target, GA_ROOT), hwnd, "pointer target must belong to isolated window");
                let lp = LPARAM(((x as u16 as u32) | ((y as u16 as u32) << 16)) as isize);
                let hit = SendMessageW(target, WM_NCHITTEST, Some(WPARAM(0)), Some(lp)).0 as u32;
                assert_eq!(hit, want, "real pointer target {target:?} blocks edge hit at ({x},{y})");
            }
            let scale = windows::Win32::UI::HiDpi::GetDpiForWindow(hwnd).max(96) as f64 / 96.0;
            for (x, y) in [(x, y), (r.left + (60.0 * scale) as i32, r.top + (80.0 * scale) as i32)] {
                let target = WindowFromPoint(POINT { x, y });
                let lp = LPARAM(((x as u16 as u32) | ((y as u16 as u32) << 16)) as isize);
                assert_eq!(GetAncestor(target, GA_ROOT), hwnd);
                assert_eq!(SendMessageW(target, WM_NCHITTEST, Some(WPARAM(0)), Some(lp)).0 as u32,
                    HTCLIENT, "resize input layer must not intercept body/sidebar controls");
            }
            let px = r.right - (69.0 * scale).round() as i32;
            let py = r.top + (16.0 * scale).round() as i32;
            let target = WindowFromPoint(POINT { x: px, y: py });
            let lp = LPARAM(((px as u16 as u32) | ((py as u16 as u32) << 16)) as isize);
            assert_eq!(GetAncestor(target, GA_ROOT), hwnd);
            let hit = SendMessageW(target, WM_NCHITTEST, Some(WPARAM(0)), Some(lp)).0 as u32;
            assert!(hit == HTTRANSPARENT as u32 || hit == HTMAXBUTTON, "resize layer covers maximize control");
            assert_eq!(SendMessageW(hwnd, WM_NCHITTEST, Some(WPARAM(0)), Some(lp)).0 as u32, HTMAXBUTTON);
        }
    }

    fn snapshot(hwnd: HWND, name: &str, scale: f64, acrylic: bool) {
        use windows::Win32::Foundation::RECT;
        use windows::Win32::Graphics::Gdi::*;
        use windows::Win32::UI::WindowsAndMessaging::GetClientRect;
        struct Capture { dc: HDC, bitmap: HBITMAP, old: HGDIOBJ }
        impl Drop for Capture {
            fn drop(&mut self) { unsafe {
                if !self.old.0.is_null() && self.old.0 as isize != -1 { let _ = SelectObject(self.dc, self.old); }
                if !self.bitmap.0.is_null() { let _ = DeleteObject(self.bitmap.into()); }
                let _ = DeleteDC(self.dc);
            } }
        }
        struct ScreenDc(HDC);
        impl Drop for ScreenDc {
            fn drop(&mut self) { unsafe { let _ = ReleaseDC(None, self.0); } }
        }
        let output = std::env::var_os("DESK_BACKDROP_TEST_OUTPUT");
        unsafe {
            let mut rect = RECT::default();
            GetClientRect(hwnd, &mut rect).unwrap();
            let (w, h) = (rect.right, rect.bottom);
            assert!(w > 0 && h > 0);
            let dc = CreateCompatibleDC(None);
            assert!(!dc.0.is_null());
            let mut capture = Capture { dc, bitmap: HBITMAP::default(), old: HGDIOBJ::default() };
            let info = BITMAPINFO { bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: w, biHeight: -h, biPlanes: 1, biBitCount: 32,
                biCompression: BI_RGB.0, ..Default::default()
            }, ..Default::default() };
            let mut bits = std::ptr::null_mut();
            let bitmap = CreateDIBSection(Some(dc), &info, DIB_RGB_COLORS, &mut bits, None, 0).unwrap();
            capture.bitmap = bitmap;
            capture.old = SelectObject(dc, bitmap.into());
            assert!(!capture.old.0.is_null() && capture.old.0 as isize != -1 && !bits.is_null());
            // PrintWindow 可能返回默认 NC 打印缓存；GetDC(hwnd) 又不包含
            // WebView2 的合成子层。因此只复制本测试 TOPMOST HWND 的客户区屏幕呈现，
            // 使用 ClientToScreen 定位，绝不截取整桌面或其他窗口区域。
            use windows::Win32::Foundation::POINT;
            use windows::Win32::UI::WindowsAndMessaging::{IsIconic, IsWindowVisible, IsZoomed};
            assert!(IsWindowVisible(hwnd).as_bool() && !IsIconic(hwnd).as_bool());
            let mut origin = POINT::default();
            assert!(ClientToScreen(hwnd, &mut origin).as_bool());
            let source = ScreenDc(GetDC(None));
            assert!(!source.0.0.is_null());
            BitBlt(dc, 0, 0, w, h, Some(source.0), origin.x, origin.y, SRCCOPY).unwrap();
            assert!(GdiFlush().as_bool(), "capture GDI flush failed");
            drop(source);
            let pixels = std::slice::from_raw_parts(bits as *const u8, (w * h * 4) as usize).to_vec();
            let mut bmp = Vec::new();
            bmp.extend_from_slice(b"BM");
            bmp.extend_from_slice(&(54 + pixels.len() as u32).to_le_bytes());
            bmp.extend_from_slice(&[0u8; 4]);
            bmp.extend_from_slice(&54u32.to_le_bytes());
            bmp.extend_from_slice(&40u32.to_le_bytes());
            bmp.extend_from_slice(&w.to_le_bytes()); bmp.extend_from_slice(&(-h).to_le_bytes());
            bmp.extend_from_slice(&1u16.to_le_bytes()); bmp.extend_from_slice(&32u16.to_le_bytes());
            bmp.extend_from_slice(&[0u8; 24]); bmp.extend_from_slice(&pixels);
            drop(capture);
            if let Some(dir) = output {
                let dir = std::path::PathBuf::from(dir);
                std::fs::create_dir_all(&dir).unwrap();
                std::fs::write(dir.join(format!("{name}.bmp")), bmp).unwrap();
            }
            // 先释放 GDI 资源及保存证据，再执行断言，失败也保留合成窗口图像。
            let (x, y) = ((400.0 * scale).round() as i32, (16.0 * scale).round() as i32);
            let i = ((y * w + x) * 4) as usize;
            let expected = if name.contains("-light-") { [237, 236, 236] } else { [33, 32, 32] };
            assert_eq!(&pixels[i..i + 3], &expected, "{name}: caption is not opaque theme color");
            // 同时采样 DWM 可见外框及客户区边缘，不能用正文正常代替 NC 白边验收。
            // Windows 11 BORDER_COLOR 是设置属性，不能用不支持的 getter 代替屏幕验收。
            {
                let light = name.contains("-light-");
                let main_color = if light { [246u32; 3] } else { [26u32; 3] };
                let caption_color = if light { [236, 236, 237] } else { [32, 32, 33] };
                let mut frame = RECT::default();
                windows::Win32::Graphics::Dwm::DwmGetWindowAttribute(hwnd,
                    windows::Win32::Graphics::Dwm::DWMWA_EXTENDED_FRAME_BOUNDS,
                    &mut frame as *mut _ as _, std::mem::size_of_val(&frame) as u32).unwrap();
                let edge_dc = ScreenDc(GetDC(None));
                let mut edges = Vec::new();
                let caption_y = origin.y + (16.0 * scale).round() as i32;
                let middle_y = (frame.top + frame.bottom) / 2;
                // 左侧材质可能随桌面变化，和相邻侧栏比较；右侧/底部/标题必须匹配不透明底色。
                // 旧 <150 阈值会让 62~76 的灰边通过，且完全漏掉标题两侧白边。
                let points = [
                    (frame.left, middle_y, None),
                    (frame.right - 1, middle_y, Some(main_color)),
                    ((frame.left + frame.right) / 2, frame.bottom - 1, Some(main_color)),
                    (frame.left, caption_y, None),
                    (frame.right - 1, caption_y, Some(caption_color)),
                    (frame.right - 3, caption_y, Some(caption_color)),
                    ((frame.left + frame.right) / 2, frame.top, Some(caption_color)),
                    (origin.x + (60.0 * scale) as i32, frame.top, None),
                ];
                let mut checks = Vec::new();
                for (x, y, expected) in points {
                    let color = GetPixel(edge_dc.0, x, y).0;
                    assert_ne!(color, u32::MAX, "edge capture failed");
                    let actual = [color & 255, (color >> 8) & 255, (color >> 16) & 255];
                    let reference = if y == frame.top {
                        GetPixel(edge_dc.0, x, y + 2).0
                    } else { GetPixel(edge_dc.0, origin.x + 2, y).0 };
                    let expected = expected.unwrap_or([reference & 255, (reference >> 8) & 255, (reference >> 16) & 255]);
                    edges.push(serde_json::json!({"x":x,"y":y,"rgb":actual,"expected":expected}));
                    checks.push((actual, expected, x, y, if x == frame.left { 8 } else { 2 }));
                }
                if let Some(dir) = std::env::var_os("DESK_BACKDROP_TEST_OUTPUT") {
                    let evidence = serde_json::json!({"visible_frame":[frame.left,frame.top,frame.right,frame.bottom],
                        "client_origin":[origin.x,origin.y],"client_size":[w,h],"visible_edges":edges,
                        "geometry_non_topmost_checked":name.contains("-dark-"),
                        "real_pointer_targets_checked":name.contains("-dark-") && !IsZoomed(hwnd).as_bool()});
                    std::fs::write(std::path::PathBuf::from(dir).join(format!("{name}-geometry.json")),
                        serde_json::to_vec_pretty(&evidence).unwrap()).unwrap();
                }
                for (actual, expected, x, y, tolerance) in checks {
                    assert!(actual.into_iter().zip(expected).all(|(a, e)| a.abs_diff(e) <= tolerance),
                        "{name}: visible frame differs from adjacent surface at ({x},{y}): {actual:?}, expected={expected:?}");
                }
                for (x, y) in [(0, h / 2), (w - 1, h / 2), (w / 2, h - 1)] {
                    let edge = ((y * w + x) * 4) as usize;
                    assert!(pixels[edge..edge + 3].iter().all(|&v| if light { v > 120 } else { v < 80 }),
                        "{name}: bright outer edge at ({x},{y}): {:?}", &pixels[edge..edge + 3]);
                }
            }
            // 必须包含真实合成 WebView，而不是全黑的父窗口 GDI 重定向面。
            let main = (((120.0 * scale).round() as i32 * w + (400.0 * scale).round() as i32) * 4) as usize;
            assert_eq!(&pixels[main..main + 3], if name.contains("-light-") { &[246, 246, 246] } else { &[26, 26, 26] }, "{name}: WebView composition absent");
            if acrylic && !name.contains("-light-") {
                for (x, y) in [(60.0, 120.0), (160.0, 300.0), (160.0, 500.0)] {
                    let (x, y) = ((x * scale).round() as i32, (y * scale).round() as i32);
                    let i = ((y * w + x) * 4) as usize;
                    assert!(pixels[i..i + 3].iter().all(|&v| v < 120), "{name}: gray native surface at ({x},{y}): {:?}", &pixels[i..i + 3]);
                }
            }
        }
    }

    #[test]
    fn capability_is_explicit_even_when_compatibility_bridge_exists() {
        assert!(script(true).contains("='acrylic'"));
        assert!(script(false).contains("='solid'"));
        assert!(script(false).contains("add('no-vibrancy')"));
    }
}
