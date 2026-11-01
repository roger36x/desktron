# 桌面壳

macOS 默认使用 Swift 与系统 WebKit。原生壳定位产品目录、启动 Python 服务、承载工作台及浏览器视图，并处理窗口与进程生命周期。

[native](native) 是 macOS 实现，[windows](windows) 是 Windows 实现(Rust、WebView2)，两者共用 [shared/js](shared/js) 中从 Swift 提取的页面脚本。Tauri 文件是其他壳方案的骨架，不能把它当作默认发行依赖或已验收的跨平台客户端。

## 与前端的边界

工作台页面由本地服务提供，浏览器由原生视图承载。打开设置和资源弹窗时，两层需要协调显示与恢复；CSS 层级只能作用于 WebView 内部。网页动作由原生桥与 App Control Runtime 协同处理。

## 图标资源

`icons/icon.png` 保存 2026-10-15 确认的台灯图稿 A 版：灯座、灯柱和横梁轻度加粗，中性深灰底、扁平白色灯身与暖黄色灯口，保留局部光晕；灯帽顶部圆弧与灯罩居中。背景仍有极轻微纹理，并非像素级纯色。运行 `python3 desktop/icons/build_macos_icon.py` 生成 macOS 多尺寸 ICNS、Windows ICO 与窗口 RGBA、网页主屏图标和两种主题的品牌图标。命令需要 Pillow，仅生成资源，不安装或激活应用。

macOS 将原图内容高度缩至 93.75% 并垂直居中，宽度与圆角底板保持不变，以增加台灯上下留白；顶部与底部延展深灰边缘，圆角底板外透明。网页和 Windows 保留完整深灰画布；手机主屏图标不带透明区域，圆角由系统裁切。只更新网页主屏资源时使用 `--home-screen-only`，不会改动 ICNS、Windows 或品牌图标。

`frontend/sidebar-brand-logo.png` 从原图提取白色台灯与黄色灯泡，移除深灰底与外围光晕，裁去留白并保持比例。侧栏品牌行当前只显示 Desktron 文字，不展示台灯；保留资源供独立使用。运行 `python3 desktop/icons/build_macos_icon.py --sidebar-only` 只生成该资源。

欢迎页标题当前仅显示“今天想让 Desk 做什么？”，不展示台灯。保留透明底 `frontend/welcome-logo.png` 与 `welcome-logo-light.png` 资源及生成入口；深色版本为白色台灯，浅色版本为深灰台灯，暖黄灯泡相同。运行 `python3 desktop/icons/build_macos_icon.py --welcome-only` 只生成这些备用资源，不改程序坞、手机主屏、Windows 或旧品牌图标。

## 构建与生效

从源码仓根运行 `bash desktron/bin/desktron app-install` 构建原生入口，默认落点为 `~/Applications/Desktron.app`。构建完成后必须使用稳定证书签署 `io.github.desktron` 并验证指定要求不含 `cdhash`；多身份机器可设置 `DESKTRON_CODESIGN_IDENTITY`。首次没有 Developer ID 或专用 Desktron 身份时，构建器会创建一次本地自签代码签名身份，macOS 可能显示钥匙串信任确认。

若目标正在运行，裸命令会拒绝原地覆盖。取得明确重启授权后，使用 `bash desktron/bin/desktron app-install --activate`：它先构建并签名到 `~/.local/share/desktron/updates/dev-host.*`，再由运行中后端提交原生宿主持有的更新 operation；失败自动恢复旧壳。发行构建通过 `DESKTRON_APPDIR` 指定暂存落点，不写入用户运行资料。

原生代码修改需要重新构建并激活后才能验收。检查新 `boot_id`、`host_instance_id`、launch generation、宿主 revision、payload revision、Remote Control、浏览器遮挡、窗口恢复、关闭行为和进程退出，不能只验证前端截图或构建成功。

相关：[界面与交互](../docs/ui-architecture.md)、[App Control](../docs/agent-operable-app.md)、[交付流程](../docs/release.md)。

Windows 壳的构建、安装与验收见 [Windows 版](../docs/windows.md)。
