//! Windows 上层窗口标题区：只绘制在主区上方，侧栏仍由整窗 WebView 通顶承载。
//!
//! 保留 WS_THICKFRAME / 系统菜单与工作区最大化；全客户区消除默认边条，独立原生层承载缩放输入。
//! 将主 WebView HWND 的标题区裁掉，使真实 WM_NCHITTEST 到达顶层窗口，而非 HTML 模拟按钮。

use std::cell::Cell;

use windows::core::w;

use tao::platform::windows::WindowExtWindows;
use tao::window::Window;
use windows::Win32::Foundation::{COLORREF, HWND, LPARAM, LRESULT, POINT, RECT, WPARAM};
use windows::Win32::Graphics::Gdi::*;
use windows::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_BORDER_COLOR};
use windows::Win32::UI::Controls::WM_MOUSELEAVE;
use windows::Win32::UI::HiDpi::{GetDpiForWindow, GetSystemMetricsForDpi};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    ReleaseCapture, SetCapture, TrackMouseEvent, TME_LEAVE, TME_NONCLIENT, TRACKMOUSEEVENT,
};
use windows::Win32::UI::Shell::{
    DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass, SHAppBarMessage,
    APPBARDATA, ABM_GETAUTOHIDEBAREX, ABE_BOTTOM, ABE_LEFT, ABE_RIGHT, ABE_TOP,
};
use windows::Win32::UI::WindowsAndMessaging::*;
use wry::{WebView, WebViewExtWindows};

pub const HEIGHT: f64 = 32.0;
const BUTTON: f64 = 46.0;
const SUBCLASS: usize = 0x44534348;

/// 纯坐标逻辑单独测试；所有值为当前 DPI 的物理像素。
#[derive(Clone, Copy, Debug)]
struct Geometry {
    width: i32,
    height: i32,
    left: i32,
    caption: i32,
    button: i32,
    top_resize: i32,
}

impl Geometry {
    fn hit(&self, x: i32, y: i32) -> u32 {
        if x < 0 || x >= self.width || y < 0 || y >= self.height {
            return HTNOWHERE;
        }
        if y < self.top_resize {
            return if x < self.top_resize {
                HTTOPLEFT
            } else if x >= self.width - self.top_resize {
                HTTOPRIGHT
            } else {
                HTTOP
            };
        }
        if x < self.left || y >= self.caption {
            return HTCLIENT;
        }
        let from_right = self.width - x;
        if from_right <= self.button {
            HTCLOSE
        } else if from_right <= self.button * 2 {
            HTMAXBUTTON
        } else if from_right <= self.button * 3 {
            HTMINBUTTON
        } else {
            HTCAPTION
        }
    }

    fn caption_rect(&self) -> RECT {
        RECT { left: self.left, top: 0, right: self.width, bottom: self.caption }
    }

    fn button_rect(&self, hit: u32) -> RECT {
        let index = match hit {
            HTCLOSE => 1,
            HTMAXBUTTON => 2,
            _ => 3,
        };
        RECT {
            left: self.width - self.button * index,
            top: 0,
            right: self.width - self.button * (index - 1),
            bottom: self.caption,
        }
    }
}

struct State {
    hwnd: HWND,
    alive: Cell<bool>,
    web: HWND,
    caption: Cell<HWND>,
    // 接管全客户区后，独立原生输入层保留八向缩放，不让WebView抢占边缘。
    resize: Cell<HWND>,
    left: Cell<f64>,
    light: Cell<bool>,
    acrylic: Cell<bool>,
    focused: Cell<bool>,
    hover: Cell<u32>,
    pressed: Cell<u32>,
}

pub struct Chrome {
    // Box 的地址在 App 移动时保持稳定，回调只有 UI 线程访问。
    state: Box<State>,
}

impl Chrome {
    pub fn new(window: &Window, web: &WebView) -> Result<Self, String> {
        let state = Box::new(State {
            hwnd: HWND(window.hwnd() as *mut _),
            alive: Cell::new(true),
            web: web.hwnd(),
            caption: Cell::new(HWND::default()),
            resize: Cell::new(HWND::default()),
            left: Cell::new(0.0),
            light: Cell::new(false),
            acrylic: Cell::new(false),
            focused: Cell::new(true),
            hover: Cell::new(HTNOWHERE),
            pressed: Cell::new(HTNOWHERE),
        });
        unsafe {
            // tao 的 undecorated-shadow 保留 WS_CAPTION；整窗扩展玻璃后 DWM
            // 仍可能画系统标题按钮。只去掉标题样式，保留缩放/系统菜单/最小最大化能力。
            let style = GetWindowLongPtrW(state.hwnd, GWL_STYLE) as u32;
            // 不加 WS_CLIPCHILDREN：透明 WebView 下方的父底图仍须清理。
            if SetWindowLongPtrW(state.hwnd, GWL_STYLE, (style & !WS_CAPTION.0) as isize) == 0 {
                return Err(format!("window_chrome_style_failed: {}", windows::core::Error::from_thread()));
            }
            // 不透明子 HWND 单独承载 GDI；不能在扩展材质的父底图上用 RGB 笔刷画标题。
            let caption = CreateWindowExW(WINDOW_EX_STYLE::default(), w!("STATIC"), w!(""),
                WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS, 0, 0, 1, 1,
                Some(state.hwnd), None, None, None)
                .map_err(|e| format!("window_chrome_caption_create_failed: {e}"))?;
            state.caption.set(caption);
            if !SetWindowSubclass(caption, Some(caption_callback), SUBCLASS, &*state as *const State as usize).as_bool() {
                let _ = DestroyWindow(caption);
                return Err("window_chrome_caption_subclass_failed".into());
            }
            if !SetWindowSubclass(state.hwnd, Some(callback), SUBCLASS, &*state as *const State as usize).as_bool() {
                let _ = RemoveWindowSubclass(caption, Some(caption_callback), SUBCLASS);
                let _ = DestroyWindow(caption);
                return Err("window_chrome_subclass_failed".into());
            }
        }
        let chrome = Self { state };
        unsafe {
            // alpha=0 的 layered 窗口会丢失鼠标命中。alpha=1只改变至多一个色阶，
            // 不绘制可见边条；窗口区域仅包含 DPI 缩放热区，内部控件不被遮挡。
            let edge = CreateWindowExW(WS_EX_LAYERED, w!("STATIC"), w!(""),
                WS_CHILD | WS_CLIPSIBLINGS, 0, 0, 1, 1,
                Some(chrome.state.hwnd), None, None, None)
                .map_err(|e| format!("window_chrome_resize_create_failed: {e}"))?;
            chrome.state.resize.set(edge);
            if !SetWindowSubclass(edge, Some(resize_callback), SUBCLASS,
                &*chrome.state as *const State as usize).as_bool() {
                return Err("window_chrome_resize_subclass_failed".into());
            }
            SetLayeredWindowAttributes(edge, COLORREF(0), 1, LWA_ALPHA)
                .map_err(|e| format!("window_chrome_resize_alpha_failed: {e}"))?;
        }
        // 自定义 NC 几何已安装后再重算 frame；不能让 tao 的旧 inset 留到第一次缩放。
        unsafe {
            SetWindowPos(chrome.state.hwnd, None, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
                .map_err(|e| format!("window_chrome_frame_failed: {e}"))?;
            // Windows 11 的默认 DWM 一像素边框独立于客户区；去掉边框但保留阴影。
            let color: u32 = 0xfffffffe; // DWMWA_COLOR_NONE
            if let Err(error) = DwmSetWindowAttribute(chrome.state.hwnd, DWMWA_BORDER_COLOR,
                &color as *const _ as _, std::mem::size_of_val(&color) as u32) {
                eprintln!("window_chrome_border_color_failed: {error}");
            }
        }
        chrome.layout()?;
        Ok(chrome)
    }

    pub fn set_left(&self, left: f64) -> Result<(), String> {
        if !left.is_finite() || !(0.0..=10000.0).contains(&left) {
            return Err("window_chrome_invalid_layout".into());
        }
        self.state.left.set(left);
        self.layout()
    }

    pub fn layout(&self) -> Result<(), String> {
        unsafe {
            self.state.clip_web()?;
            let g = self.state.geometry();
            SetWindowPos(self.state.caption.get(), Some(HWND_TOP), g.left, 0,
                (g.width - g.left).max(0), g.caption, SWP_NOACTIVATE)
                .map_err(|e| format!("window_chrome_caption_layout_failed: {e}"))?;
            self.state.layout_resize()?;
            self.state.invalidate();
        }
        Ok(())
    }

    /// Browser/tag视图置顶后再次保护原生边缘输入，不改变页面内部叠层。
    pub fn raise_resize(&self) -> Result<(), String> {
        if !self.state.alive.get() { return Ok(()); }
        unsafe {
            SetWindowPos(self.state.resize.get(), Some(HWND_TOP), 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                .map_err(|e| format!("window_chrome_resize_raise_failed: {e}"))
        }
    }

    #[cfg(test)]
    pub(crate) fn owned_windows_destroyed(&self) -> bool {
        !self.state.alive.get() && self.state.caption.get().0.is_null() && self.state.resize.get().0.is_null()
    }

    pub fn set_acrylic(&self, acrylic: bool) {
        self.state.acrylic.set(acrylic);
        // 状态改变或回退必须刷新整张父窗口底图，而非只刷新标题区。
        unsafe { self.state.invalidate(); }
    }

    pub fn set_light(&self, light: bool) {
        self.state.light.set(light);
        unsafe { self.state.invalidate() }
    }
}

impl Drop for Chrome {
    fn drop(&mut self) {
        unsafe {
            if self.state.alive.replace(false) {
                let _ = RemoveWindowSubclass(self.state.hwnd, Some(callback), SUBCLASS);
            }
            let caption = self.state.caption.replace(HWND::default());
            if !caption.0.is_null() {
                let _ = RemoveWindowSubclass(caption, Some(caption_callback), SUBCLASS);
                let _ = DestroyWindow(caption);
            }
            let resize = self.state.resize.replace(HWND::default());
            if !resize.0.is_null() {
                let _ = RemoveWindowSubclass(resize, Some(resize_callback), SUBCLASS);
                let _ = DestroyWindow(resize);
            }
        }
    }
}

impl State {
    unsafe fn geometry(&self) -> Geometry {
        let mut rect = RECT::default();
        let _ = GetClientRect(self.hwnd, &mut rect);
        let dpi = GetDpiForWindow(self.hwnd).max(96);
        let scale = dpi as f64 / 96.0;
        let px = |value: f64| (value * scale).round() as i32;
        Geometry {
            width: rect.right,
            height: rect.bottom,
            left: px(self.left.get()).clamp(0, rect.right.max(0)),
            caption: px(HEIGHT),
            button: px(BUTTON),
            // 缩放输入已由独立层承载，不再挖出缺少侧栏CSS底色的顶沿。
            top_resize: 0,
        }
    }

    unsafe fn resize_hit(&self, x: i32, y: i32) -> u32 {
        if IsZoomed(self.hwnd).as_bool() || GetWindowLongPtrW(self.hwnd, GWL_STYLE) as u32 & WS_THICKFRAME.0 == 0 {
            return HTCLIENT;
        }
        let g = self.geometry();
        let dpi = GetDpiForWindow(self.hwnd).max(96);
        let padded = GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi);
        resize_hit(g.width, g.height, x, y,
            GetSystemMetricsForDpi(SM_CXFRAME, dpi) + padded,
            GetSystemMetricsForDpi(SM_CYFRAME, dpi) + padded)
    }

    unsafe fn layout_resize(&self) -> Result<(), String> {
        let edge = self.resize.get();
        if edge.0.is_null() || !self.alive.get() { return Ok(()); }
        if IsZoomed(self.hwnd).as_bool() || IsIconic(self.hwnd).as_bool() {
            let _ = ShowWindow(edge, SW_HIDE);
            return Ok(());
        }
        let g = self.geometry();
        if g.width <= 0 || g.height <= 0 { return Ok(()); }
        let dpi = GetDpiForWindow(self.hwnd).max(96);
        let padded = GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi);
        let bx = GetSystemMetricsForDpi(SM_CXFRAME, dpi) + padded;
        let by = GetSystemMetricsForDpi(SM_CYFRAME, dpi) + padded;
        let region = CreateRectRgn(0, 0, g.width, g.height);
        let inside = CreateRectRgn(bx, by, (g.width - bx).max(bx), (g.height - by).max(by));
        if region.0.is_null() || inside.0.is_null() {
            let _ = DeleteObject(region.into()); let _ = DeleteObject(inside.into());
            return Err("window_chrome_resize_region_create_failed".into());
        }
        let combined = CombineRgn(Some(region), Some(region), Some(inside), RGN_DIFF);
        let _ = DeleteObject(inside.into());
        if combined.0 == 0 || SetWindowRgn(edge, Some(region), true) == 0 {
            let _ = DeleteObject(region.into());
            return Err("window_chrome_resize_region_apply_failed".into());
        }
        SetWindowPos(edge, Some(HWND_TOP), 0, 0, g.width, g.height,
            SWP_NOACTIVATE | SWP_SHOWWINDOW)
            .map_err(|e| format!("window_chrome_resize_layout_failed: {e}"))?;
        Ok(())
    }

    unsafe fn clip_web(&self) -> Result<(), String> {
        let g = self.geometry();
        if g.width <= 0 || g.height <= 0 {
            return Ok(());
        }
        // 只让出不透明caption；侧栏及所有边缘由页面完整覆盖，缩放层另行处理输入。
        let region = CreateRectRgn(0, g.top_resize, g.width, g.height);
        let caption = CreateRectRgn(g.left, 0, g.width, g.caption);
        if region.0.is_null() || caption.0.is_null() {
            let _ = DeleteObject(region.into());
            let _ = DeleteObject(caption.into());
            return Err("window_chrome_region_create_failed".into());
        }
        let combined = CombineRgn(Some(region), Some(region), Some(caption), RGN_DIFF);
        let _ = DeleteObject(caption.into());
        if combined.0 == 0 || SetWindowRgn(self.web, Some(region), true) == 0 {
            let _ = DeleteObject(region.into());
            return Err("window_chrome_region_apply_failed".into());
        }
        // SetWindowRgn 成功后系统拥有 region，不得再 DeleteObject。
        Ok(())
    }

    unsafe fn invalidate(&self) {
        if !self.alive.get() { return; }
        let _ = InvalidateRect(Some(self.hwnd), None, false);
        let _ = InvalidateRect(Some(self.caption.get()), None, false);
        let _ = InvalidateRect(Some(self.resize.get()), None, false);
    }

    unsafe fn cursor_hit(&self) -> u32 {
        let mut point = POINT::default();
        if GetCursorPos(&mut point).is_err() || !ScreenToClient(self.hwnd, &mut point).as_bool() {
            return HTNOWHERE;
        }
        self.geometry().hit(point.x, point.y)
    }

    unsafe fn paint(&self) {
        let mut paint = PAINTSTRUCT::default();
        let dc = BeginPaint(self.hwnd, &mut paint);
        // tao 窗口使用 CS_OWNDC；EndPaint 不会重置应用设置的裁剪区。
        // 每帧恢复 DC，避免旧窗口宽度及侧栏左缘限制之后的绘制。
        if let Err(error) = with_saved_dc(dc, |dc| self.clear_backdrop(dc)) {
            eprintln!("{error}");
        }
        let _ = EndPaint(self.hwnd, &paint);
    }

    unsafe fn clear_backdrop(&self, dc: HDC) -> Result<(), &'static str> {
        if self.acrylic.get() {
            let g = self.geometry();
            // 只排除当前不透明 caption，不能排除透明 WebView；旧标题范围仍会清理。
            if ExcludeClipRect(dc, g.left, 0, g.width, g.caption).0 == 0 { return Err("window_chrome_clear_clip_failed"); }
            fill(dc, RECT { left: 0, top: 0, right: g.width, bottom: g.height }, rgb(0, 0, 0))?;
        }
        Ok(())
    }

    unsafe fn paint_caption(&self) {
        let mut paint = PAINTSTRUCT::default();
        let dc = BeginPaint(self.caption.get(), &mut paint);
        self.draw_caption_to(dc);
        let _ = EndPaint(self.caption.get(), &paint);
    }

    unsafe fn draw_caption_to(&self, dc: HDC) {
        let g = self.geometry();
        if let Err(error) = paint_opaque(dc, (g.width - g.left).max(0), g.caption, |surface| {
            if !SetViewportOrgEx(surface, -g.left, 0, None).as_bool() { return Err("window_chrome_viewport_failed"); }
            self.draw(surface)
        }) { eprintln!("{error}"); }
    }

    unsafe fn draw(&self, dc: HDC) -> Result<(), &'static str> {
        let g = self.geometry();
        let light = self.light.get();
        let background = if light { rgb(236, 236, 237) } else { rgb(32, 32, 33) };
        let text = if !self.focused.get() {
            rgb(130, 130, 134)
        } else if light {
            rgb(65, 65, 69)
        } else {
            rgb(201, 201, 205)
        };
        // 此 DC 属于不透明 caption 子窗口，不参与父窗口材质 alpha 合成。
        if IntersectClipRect(dc, g.left, 0, g.width, g.caption).0 == 0 { return Err("window_chrome_caption_clip_failed"); }
        fill(dc, g.caption_rect(), background)?;
        // 上层只保留拖动留白与窗口按钮；品牌由侧栏展示，系统窗口名称仍在 tao 保留。
        for hit in [HTMINBUTTON, HTMAXBUTTON, HTCLOSE] {
            let rect = g.button_rect(hit);
            let hovered = self.hover.get() == hit;
            let pressed = self.pressed.get() == hit && hovered;
            if hovered {
                let color = if hit == HTCLOSE {
                    if pressed {
                        rgb(160, 30, 20)
                    } else {
                        rgb(196, 43, 28)
                    }
                } else if light {
                    if pressed {
                        rgb(207, 207, 210)
                    } else {
                        rgb(219, 219, 222)
                    }
                } else if pressed {
                    rgb(57, 57, 61)
                } else {
                    rgb(48, 48, 51)
                };
                fill(dc, rect, color)?;
            }
            let ink = if hit == HTCLOSE && hovered { rgb(255, 255, 255) } else { text };
            let scale = GetDpiForWindow(self.hwnd).max(96) as f64 / 96.0;
            let cx = (rect.left + rect.right) / 2;
            let cy = (rect.top + rect.bottom) / 2;
            let d = (4.5 * scale).round() as i32;
            let pen = CreatePen(PS_SOLID, scale.round().max(1.0) as i32, ink);
            if pen.0.is_null() { return Err("window_chrome_pen_create_failed"); }
            let old_pen = SelectObject(dc, pen.into());
            if old_pen.0.is_null() || old_pen.0 as isize == -1 {
                let _ = DeleteObject(pen.into());
                return Err("window_chrome_pen_select_failed");
            }
            let result = if hit == HTMINBUTTON {
                line(dc, cx - d, cy, cx + d, cy)
            } else if hit == HTCLOSE {
                line(dc, cx - d, cy - d, cx + d, cy + d)
                    .and_then(|()| line(dc, cx + d, cy - d, cx - d, cy + d))
            } else if IsZoomed(self.hwnd).as_bool() {
                let offset = (2.0 * scale).round() as i32;
                line(dc, cx - d + offset, cy - d, cx + d, cy - d)
                    .and_then(|()| line(dc, cx + d, cy - d, cx + d, cy + d - offset))
                    .and_then(|()| outline(dc, cx - d, cy - d + offset, cx + d - offset, cy + d))
            } else {
                outline(dc, cx - d, cy - d, cx + d, cy + d)
            };
            let _ = SelectObject(dc, old_pen);
            let _ = DeleteObject(pen.into());
            result?;
        }
        Ok(())
    }
}

// 扩展玻璃父窗口上的普通 GDI 会留下 alpha=0，即使在子 HWND 上 RGB 正确，
// DWM 仍可能将标题混入 Acrylic。先画到 BGRA DIB，明确置 alpha=255 再拷贝。
unsafe fn paint_opaque(dc: HDC, width: i32, height: i32, draw: impl FnOnce(HDC) -> Result<(), &'static str>) -> Result<(), &'static str> {
    if width <= 0 || height <= 0 { return Ok(()); }
    let surface = CreateCompatibleDC(Some(dc));
    if surface.0.is_null() { return Err("window_chrome_surface_dc_failed"); }
    let info = BITMAPINFO { bmiHeader: BITMAPINFOHEADER {
        biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
        biWidth: width, biHeight: -height, biPlanes: 1, biBitCount: 32,
        biCompression: BI_RGB.0, ..Default::default()
    }, ..Default::default() };
    let mut bits = std::ptr::null_mut();
    let bitmap = match CreateDIBSection(Some(surface), &info, DIB_RGB_COLORS, &mut bits, None, 0) {
        Ok(bitmap) => bitmap,
        Err(_) => { let _ = DeleteDC(surface); return Err("window_chrome_surface_bitmap_failed"); }
    };
    let old = SelectObject(surface, bitmap.into());
    if old.0.is_null() || old.0 as isize == -1 || bits.is_null() {
        if !old.0.is_null() && old.0 as isize != -1 { let _ = SelectObject(surface, old); }
        let _ = DeleteObject(bitmap.into());
        let _ = DeleteDC(surface);
        return Err("window_chrome_surface_select_failed");
    }
    let result = with_saved_dc(surface, draw).and_then(|()| {
        // GDI 操作可能批处理；在直接访问 DIB 内存前必须完成提交。
        if !GdiFlush().as_bool() { return Err("window_chrome_surface_flush_failed"); }
        let pixels = std::slice::from_raw_parts_mut(bits as *mut u8, width as usize * height as usize * 4);
        for pixel in pixels.chunks_exact_mut(4) { pixel[3] = 255; }
        BitBlt(dc, 0, 0, width, height, Some(surface), 0, 0, SRCCOPY)
            .map_err(|_| "window_chrome_surface_copy_failed")
    });
    let _ = SelectObject(surface, old);
    let _ = DeleteObject(bitmap.into());
    let _ = DeleteDC(surface);
    result
}

// 同一 DC 上允许窗口放大、缩小及侧栏开合，不能让应用裁剪跨帧累积。
unsafe fn with_saved_dc(dc: HDC, draw: impl FnOnce(HDC) -> Result<(), &'static str>) -> Result<(), &'static str> {
    let saved = SaveDC(dc);
    if saved == 0 {
        return Err("window_chrome_dc_save_failed");
    }
    let result = draw(dc);
    if !RestoreDC(dc, saved).as_bool() {
        return Err("window_chrome_dc_restore_failed");
    }
    result
}

fn rgb(r: u8, g: u8, b: u8) -> COLORREF {
    COLORREF(r as u32 | ((g as u32) << 8) | ((b as u32) << 16))
}

unsafe fn fill(dc: HDC, rect: RECT, color: COLORREF) -> Result<(), &'static str> {
    let brush = CreateSolidBrush(color);
    if brush.0.is_null() { return Err("window_chrome_brush_create_failed"); }
    let filled = FillRect(dc, &rect, brush);
    let _ = DeleteObject(brush.into());
    if filled == 0 { Err("window_chrome_fill_failed") } else { Ok(()) }
}

unsafe fn line(dc: HDC, x1: i32, y1: i32, x2: i32, y2: i32) -> Result<(), &'static str> {
    if !MoveToEx(dc, x1, y1, None).as_bool() || !LineTo(dc, x2, y2).as_bool() {
        return Err("window_chrome_line_failed");
    }
    Ok(())
}

unsafe fn outline(dc: HDC, left: i32, top: i32, right: i32, bottom: i32) -> Result<(), &'static str> {
    line(dc, left, top, right, top)?;
    line(dc, right, top, right, bottom)?;
    line(dc, right, bottom, left, bottom)?;
    line(dc, left, bottom, left, top)
}

unsafe extern "system" fn caption_callback(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM, _id: usize, data: usize) -> LRESULT {
    let state = &*(data as *const State);
    match msg {
        WM_PAINT => { state.paint_caption(); return LRESULT(0); }
        WM_PRINT | WM_PRINTCLIENT => { state.draw_caption_to(HDC(wp.0 as *mut _)); return LRESULT(0); }
        WM_ERASEBKGND => return LRESULT(1),
        // 绘制与命中分开：让同线程父窗口继续处理拖动、缩放和真实系统按钮。
        WM_NCHITTEST => return LRESULT(HTTRANSPARENT as isize),
        WM_NCDESTROY => {
            state.caption.set(HWND::default());
            let _ = RemoveWindowSubclass(hwnd, Some(caption_callback), SUBCLASS);
        }
        _ => {}
    }
    DefSubclassProc(hwnd, msg, wp, lp)
}

// 输入层返回真实NC命中，并把系统缩放动作转交顶层窗口。
// 不能只返回 HTTRANSPARENT：下方WebView自身也可能接收命中，抢占父窗口边缘。
unsafe extern "system" fn resize_callback(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM, _id: usize, data: usize) -> LRESULT {
    let state = &*(data as *const State);
    match msg {
        WM_PAINT => {
            let mut paint = PAINTSTRUCT::default();
            let dc = BeginPaint(hwnd, &mut paint);
            let mut rect = RECT::default();
            if GetClientRect(hwnd, &mut rect).is_ok() {
                if let Err(error) = fill(dc, rect, rgb(0, 0, 0)) { eprintln!("{error}"); }
            }
            let _ = EndPaint(hwnd, &paint);
            return LRESULT(0);
        }
        WM_ERASEBKGND => return LRESULT(1),
        WM_NCHITTEST => {
            let mut p = POINT { x: lp.0 as i16 as i32, y: (lp.0 >> 16) as i16 as i32 };
            if ScreenToClient(state.hwnd, &mut p).as_bool() {
                let hit = state.resize_hit(p.x, p.y);
                if hit != HTCLIENT { return LRESULT(hit as isize); }
            }
            return LRESULT(HTTRANSPARENT as isize);
        }
        WM_SETCURSOR => return DefWindowProcW(state.hwnd, msg, WPARAM(state.hwnd.0 as usize), lp),
        WM_NCLBUTTONDOWN | WM_NCLBUTTONDBLCLK | WM_NCRBUTTONDOWN | WM_NCRBUTTONUP => {
            return SendMessageW(state.hwnd, msg, Some(wp), Some(lp));
        }
        WM_NCDESTROY => {
            state.resize.set(HWND::default());
            let _ = RemoveWindowSubclass(hwnd, Some(resize_callback), SUBCLASS);
        }
        _ => {}
    }
    DefSubclassProc(hwnd, msg, wp, lp)
}

// 显式命中保留八向边角，优先于 tao shadow 分支对整条上沿的 HTTOP。
fn resize_hit(width: i32, height: i32, x: i32, y: i32, bx: i32, by: i32) -> u32 {
    if x < 0 || y < 0 || x >= width || y >= height { return HTCLIENT; }
    let left = x < bx;
    let right = x >= width - bx;
    let top = y < by;
    let bottom = y >= height - by;
    match (left, right, top, bottom) {
        (true, _, true, _) => HTTOPLEFT,
        (_, true, true, _) => HTTOPRIGHT,
        (true, _, _, true) => HTBOTTOMLEFT,
        (_, true, _, true) => HTBOTTOMRIGHT,
        (true, _, _, _) => HTLEFT,
        (_, true, _, _) => HTRIGHT,
        (_, _, true, _) => HTTOP,
        (_, _, _, true) => HTBOTTOM,
        _ => HTCLIENT,
    }
}

// 工作区使用屏幕物理坐标；ptMaxPosition 必须换成相对当前显示器的坐标。
fn constrain_maximize(info: &mut MINMAXINFO, monitor: RECT, work: RECT) {
    info.ptMaxPosition = POINT { x: work.left - monitor.left, y: work.top - monitor.top };
    info.ptMaxSize = POINT { x: work.right - work.left, y: work.bottom - work.top };
    // 不改 tao 已应用的最小/最大拖动尺寸约束。
}

unsafe fn monitor_work_area(hwnd: HWND) -> Result<(RECT, RECT), &'static str> {
    let mut info = MONITORINFO { cbSize: std::mem::size_of::<MONITORINFO>() as u32, ..Default::default() };
    if !GetMonitorInfoW(MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), &mut info).as_bool() {
        return Err("window_chrome_monitor_info_failed");
    }
    let mut work = info.rcWork;
    // 自动隐藏任务栏的工作区可能等于整屏，按当前显示器保留一物理像素唤出带。
    for edge in [ABE_LEFT, ABE_TOP, ABE_RIGHT, ABE_BOTTOM] {
        let mut bar = APPBARDATA { cbSize: std::mem::size_of::<APPBARDATA>() as u32,
            uEdge: edge, rc: info.rcMonitor, ..Default::default() };
        if SHAppBarMessage(ABM_GETAUTOHIDEBAREX, &mut bar) != 0 {
            match edge {
                ABE_LEFT => work.left += 1,
                ABE_TOP => work.top += 1,
                ABE_RIGHT => work.right -= 1,
                _ => work.bottom -= 1,
            }
        }
    }
    if work.right <= work.left || work.bottom <= work.top {
        return Err("window_chrome_invalid_work_area");
    }
    Ok((info.rcMonitor, work))
}

unsafe extern "system" fn callback(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM, _id: usize, data: usize) -> LRESULT {
    let state = &*(data as *const State);
    match msg {
        WM_GETMINMAXINFO if lp.0 != 0 => {
            let base = DefSubclassProc(hwnd, msg, wp, lp);
            match monitor_work_area(hwnd) {
                Ok((monitor, work)) => constrain_maximize(&mut *(lp.0 as *mut MINMAXINFO), monitor, work),
                Err(error) => eprintln!("{error}"),
            }
            return base;
        }
        WM_NCCALCSIZE if lp.0 != 0 => {
            // 两种 wParam 形式均不缩客户区。普通NC的默认绘制会留下灰边/标题侧白边；
            // 全客户区由页面与不透明caption覆盖，八向输入由专用原生层承担。
            // 最大化outer仍由WM_GETMINMAXINFO限定在工作区。
            return LRESULT(0);
        }
        WM_PAINT => {
            // DefWindowProc 会验证更新区；先 BeginPaint，否则其后的 DC 已无可绘区域。
            // 绘完仍转交 tao，保留重绘事件与模态循环，不截断宿主消息链。
            state.paint();
            return DefSubclassProc(hwnd, msg, wp, lp);
        }
        WM_ERASEBKGND => {
            // DWM 材质底图必须真正清零，不允许只返回“已擦除”。
            if let Err(error) = with_saved_dc(HDC(wp.0 as *mut _), |dc| state.clear_backdrop(dc)) {
                eprintln!("{error}");
                return LRESULT(0);
            }
            return LRESULT(1);
        }
        WM_NCHITTEST => {
            // 八向缩放优先于标题热区，也优先于 tao shadow 分支的 HTTOP。
            let base = DefSubclassProc(hwnd, msg, wp, lp);
            let mut point = POINT { x: lp.0 as i16 as i32, y: (lp.0 >> 16) as i16 as i32 };
            if ScreenToClient(hwnd, &mut point).as_bool() {
                let resize = state.resize_hit(point.x, point.y);
                if resize != HTCLIENT { return LRESULT(resize as isize); }
                let hit = state.geometry().hit(point.x, point.y);
                if hit != HTCLIENT && hit != HTNOWHERE {
                    return LRESULT(hit as isize);
                }
            }
            return base;
        }
        WM_NCMOUSEMOVE | WM_MOUSEMOVE => {
            let hit = state.cursor_hit();
            if state.hover.replace(hit) != hit {
                state.invalidate();
            }
            let mut track = TRACKMOUSEEVENT {
                cbSize: std::mem::size_of::<TRACKMOUSEEVENT>() as u32,
                dwFlags: if msg == WM_NCMOUSEMOVE { TME_LEAVE | TME_NONCLIENT } else { TME_LEAVE },
                hwndTrack: hwnd,
                ..Default::default()
            };
            let _ = TrackMouseEvent(&mut track);
            // 转交系统的非客户区悬停处理，HTMAXBUTTON 为 Windows 11 吸附布局的入口。
        }
        WM_NCMOUSELEAVE | WM_MOUSELEAVE => {
            state.hover.set(HTNOWHERE);
            state.invalidate();
        }
        WM_NCLBUTTONDOWN if [HTMINBUTTON, HTMAXBUTTON, HTCLOSE].contains(&(wp.0 as u32)) => {
            state.pressed.set(wp.0 as u32);
            state.hover.set(wp.0 as u32);
            let _ = SetCapture(hwnd);
            state.invalidate();
            return LRESULT(0);
        }
        WM_LBUTTONUP if state.pressed.get() != HTNOWHERE => {
            let pressed = state.pressed.replace(HTNOWHERE);
            let hit = state.cursor_hit();
            let _ = ReleaseCapture();
            state.invalidate();
            if hit == pressed {
                let command = match pressed {
                    HTMINBUTTON => SC_MINIMIZE,
                    HTMAXBUTTON if IsZoomed(hwnd).as_bool() => SC_RESTORE,
                    HTMAXBUTTON => SC_MAXIMIZE,
                    _ => SC_CLOSE,
                };
                // SC_CLOSE 最终经过 tao CloseRequested，保留 App::request_quit 的回合保护。
                let _ = PostMessageW(Some(hwnd), WM_SYSCOMMAND, WPARAM(command as usize), LPARAM(0));
            }
            return LRESULT(0);
        }
        WM_CAPTURECHANGED => {
            state.pressed.set(HTNOWHERE);
            state.invalidate();
        }
        WM_ACTIVATE => {
            state.focused.set((wp.0 & 0xffff) != WA_INACTIVE as usize);
            state.invalidate();
        }
        WM_STYLECHANGING if wp.0 == GWL_STYLE.0 as isize as usize => {
            // tao 在最大化/还原等 flags 更新时会重新生成 WS_CAPTION。
            // 在提交样式前去掉标题部分，不改变缩放和系统命令能力。
            if lp.0 != 0 {
                let styles = &mut *(lp.0 as *mut STYLESTRUCT);
                styles.styleNew &= !WS_CAPTION.0;
            }
        }
        WM_STYLECHANGED if wp.0 == GWL_STYLE.0 as isize as usize => {
            if let Err(error) = SetWindowPos(hwnd, None, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED) {
                eprintln!("window_chrome_frame_refresh_failed: {error}");
            }
            state.invalidate();
        }
        WM_SIZE | WM_DPICHANGED => {
            // 原生模态缩放期间也要同步边缘位置；不能只等tao异步Resized事件。
            if let Err(error) = state.layout_resize() { eprintln!("{error}"); }
            state.hover.set(HTNOWHERE);
            state.invalidate();
        }
        WM_NCDESTROY => {
            state.alive.set(false);
            let _ = RemoveWindowSubclass(hwnd, Some(callback), SUBCLASS);
        }
        _ => {}
    }
    // 标题区拖动、双击、右键系统菜单、Alt+Space 与 Win+方向键均走原生消息链。
    DefSubclassProc(hwnd, msg, wp, lp)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn geometry(scale: f64, left: f64, _maximized: bool) -> Geometry {
        Geometry {
            width: (1160.0 * scale).round() as i32,
            height: (860.0 * scale).round() as i32,
            left: (left * scale).round() as i32,
            caption: (HEIGHT * scale).round() as i32,
            button: (BUTTON * scale).round() as i32,
            top_resize: 0,
        }
    }

    #[test]
    fn native_frame_keeps_eight_way_resize_at_multiple_dpi() {
        for scale in [1, 2, 3] {
            let (w, h, b) = (900 * scale, 600 * scale, 8 * scale);
            for (x, y, hit) in [(1, 1, HTTOPLEFT), (w - 2, 1, HTTOPRIGHT),
                (1, h - 2, HTBOTTOMLEFT), (w - 2, h - 2, HTBOTTOMRIGHT),
                (1, h / 2, HTLEFT), (w - 2, h / 2, HTRIGHT),
                (w / 2, 1, HTTOP), (w / 2, h - 2, HTBOTTOM), (w / 2, h / 2, HTCLIENT)] {
                assert_eq!(resize_hit(w, h, x, y, b, b), hit);
            }
        }
    }

    #[test]
    fn maximized_outer_rect_uses_work_area_on_every_monitor_edge() {
        for monitor in [RECT { left: 0, top: 0, right: 2560, bottom: 1600 },
            RECT { left: -1920, top: -200, right: 0, bottom: 880 }] {
            for edge in [ABE_LEFT, ABE_TOP, ABE_RIGHT, ABE_BOTTOM] {
                let mut work = monitor;
                match edge { ABE_LEFT => work.left += 72, ABE_TOP => work.top += 72,
                    ABE_RIGHT => work.right -= 72, _ => work.bottom -= 72 }
                let mut info = MINMAXINFO { ptMinTrackSize: POINT { x: 900, y: 600 },
                    ptMaxTrackSize: POINT { x: 5000, y: 5000 }, ..Default::default() };
                constrain_maximize(&mut info, monitor, work);
                assert_eq!(monitor.left + info.ptMaxPosition.x, work.left);
                assert_eq!(monitor.top + info.ptMaxPosition.y, work.top);
                assert_eq!(info.ptMaxSize.x, work.right - work.left);
                assert_eq!(info.ptMaxSize.y, work.bottom - work.top);
                assert_eq!(info.ptMinTrackSize.x, 900);
                assert_eq!(info.ptMaxTrackSize.x, 5000);
            }
        }
    }

    #[test]
    fn persistent_dc_clip_does_not_limit_larger_caption_or_collapsed_sidebar() {
        // 内存 DC 与 CS_OWNDC 一样跨调用保留应用裁剪；不创建窗口，不读桌面。
        unsafe {
            let dc = CreateCompatibleDC(None);
            assert!(!dc.0.is_null());
            let bitmap = CreateBitmap(1800, 80, 1, 32, None);
            assert!(!bitmap.0.is_null());
            let old = SelectObject(dc, bitmap.into());
            let whole = RECT { left: 0, top: 0, right: 1800, bottom: 80 };
            let background = rgb(32, 32, 33);
            fill(dc, whole, rgb(0, 0, 0)).unwrap();

            // 复现旧行为：只 IntersectClipRect 时，新右缘无法绘制。
            let saved = SaveDC(dc);
            assert_ne!(saved, 0);
            let _ = IntersectClipRect(dc, 248, 0, 1160, 32);
            let _ = IntersectClipRect(dc, 248, 0, 1800, 32);
            fill(dc, whole, background).unwrap();
            // GetPixel 对被裁掉的区域返回 CLR_INVALID；解除裁剪后核对它仍为黑色。
            assert_eq!(GetPixel(dc, 1750, 16), COLORREF(u32::MAX));
            assert!(RestoreDC(dc, saved).as_bool());
            assert_eq!(GetPixel(dc, 1750, 16), rgb(0, 0, 0));

            // 同一个 DC 上反复还原/放大，并收起侧栏，每次都恢复帧前裁剪状态。
            for (width, left) in [(1160, 248), (1800, 248), (1160, 248), (1800, 248), (1800, 0)] {
                let mut before = RECT::default();
                let _ = GetClipBox(dc, &mut before);
                with_saved_dc(dc, |dc| {
                    let _ = IntersectClipRect(dc, left, 0, width, 32);
                    fill(dc, RECT { left, top: 0, right: width, bottom: 32 }, background)
                })
                .unwrap();
                assert_eq!(GetPixel(dc, width - 10, 16), background);
                assert_eq!(GetPixel(dc, left + 10, 16), background);
                let mut after = RECT::default();
                let _ = GetClipBox(dc, &mut after);
                assert_eq!(
                    (before.left, before.top, before.right, before.bottom),
                    (after.left, after.top, after.right, after.bottom)
                );
            }
            let _ = SelectObject(dc, old);
            let _ = DeleteObject(bitmap.into());
            assert!(DeleteDC(dc).as_bool());
        }
    }

    #[test]
    fn failed_caption_draw_is_not_committed_as_opaque() {
        unsafe {
            let dc = CreateCompatibleDC(None);
            let bitmap = CreateBitmap(64, 32, 1, 32, None);
            let old = SelectObject(dc, bitmap.into());
            let background = rgb(22, 23, 24);
            fill(dc, RECT { left: 0, top: 0, right: 64, bottom: 32 }, background).unwrap();
            let result = paint_opaque(dc, 64, 32, |surface| {
                fill(surface, RECT { left: 0, top: 0, right: 64, bottom: 32 }, rgb(255, 0, 0))?;
                Err("injected_caption_draw_failure")
            });
            assert_eq!(result, Err("injected_caption_draw_failure"));
            assert_eq!(GetPixel(dc, 16, 16), background);
            let _ = SelectObject(dc, old);
            let _ = DeleteObject(bitmap.into());
            let _ = DeleteDC(dc);
        }
    }

    #[test]
    fn caption_never_crosses_sidebar_at_all_supported_dpi() {
        for scale in [1.0, 1.25, 1.5, 2.0] {
            let g = geometry(scale, 248.0, false);
            assert_eq!(g.hit((100.0 * scale) as i32, (20.0 * scale) as i32), HTCLIENT);
            assert_eq!(g.hit((300.0 * scale) as i32, (20.0 * scale) as i32), HTCAPTION);
            assert_eq!(g.hit((300.0 * scale) as i32, (50.0 * scale) as i32), HTCLIENT);
            assert_eq!(g.hit((100.0 * scale) as i32, 0), HTCLIENT);
            assert_eq!(g.hit(0, 0), HTCLIENT);
            assert_eq!(g.hit(g.width - 1, 0), HTCLOSE);
            assert_eq!(g.caption_rect().left, g.left);
        }
    }

    #[test]
    fn collapse_extends_caption_to_left_edge() {
        let g = geometry(1.0, 0.0, true);
        assert_eq!(g.caption_rect().left, 0);
        assert_eq!(g.hit(10, 16), HTCAPTION);
    }

    #[test]
    fn controls_have_native_hit_codes_and_half_open_edges() {
        let g = geometry(1.0, 248.0, true);
        assert_eq!(g.hit(1159, 16), HTCLOSE);
        assert_eq!(g.hit(1113, 16), HTMAXBUTTON);
        assert_eq!(g.hit(1067, 16), HTMINBUTTON);
        assert_eq!(g.hit(1021, 16), HTCAPTION);
        assert_eq!(g.hit(1114, 16), HTCLOSE);
        assert_eq!(g.hit(1068, 16), HTMAXBUTTON);
        assert_eq!(g.hit(1022, 16), HTMINBUTTON);
        assert_eq!(g.hit(1160, 16), HTNOWHERE);
        assert_eq!(g.hit(-1, 16), HTNOWHERE);
        assert_eq!(g.hit(500, 32), HTCLIENT);
    }
}
