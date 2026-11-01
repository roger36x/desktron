// 在 Windows 主机上构建时嵌入图标、版本信息与应用清单(Common Controls v6、Per-Monitor DPI)。
// 其他主机交叉检查(cargo check --target x86_64-pc-windows-msvc)时没有资源编译器，跳过嵌入。
fn main() {
    println!("cargo:rerun-if-changed=icons/desktron.ico");
    println!("cargo:rerun-if-changed=desktron.manifest");
    let target_windows = std::env::var("CARGO_CFG_TARGET_OS").map(|v| v == "windows").unwrap_or(false);
    let host_windows = std::env::var("HOST").map(|v| v.contains("windows")).unwrap_or(false);
    if !target_windows || !host_windows || std::env::var_os("DESKTRON_SKIP_RESOURCES").is_some() {
        return;
    }
    let mut resource = winresource::WindowsResource::new();
    resource.set_icon("icons/desktron.ico");
    resource.set_manifest_file("desktron.manifest");
    resource.set("ProductName", "Desktron");
    resource.set("FileDescription", "Desktron");
    resource.set("CompanyName", "Desktron");
    if let Err(error) = resource.compile() {
        panic!("Windows 资源编译失败：{error}");
    }
}
