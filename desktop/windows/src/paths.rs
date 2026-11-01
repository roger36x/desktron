//! 产品目录定位。布局与 macOS 相同，根在 `%USERPROFILE%\.local\share\desktron`。

use std::path::{Path, PathBuf};

pub fn home() -> PathBuf {
    std::env::var_os("USERPROFILE").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."))
}

pub fn share() -> PathBuf {
    home().join(".local").join("share").join("desktron")
}

pub fn data_home() -> PathBuf {
    std::env::var_os("DESKTRON_DATA_HOME").map(PathBuf::from).unwrap_or_else(|| share().join("data"))
}

/// 宿主控制目录：重启与更新请求、响应和生命周期锁。与后端 lifecycle.py 一致。
pub fn control_root() -> PathBuf {
    home().join(".local").join("state").join("desktron").join("host-control")
}

pub fn logs_dir() -> PathBuf {
    data_home().join("logs")
}

/// 原生壳旁的资源文件(release-rev、release-build、desktron_home)。
pub fn exe_dir() -> PathBuf {
    std::env::current_exe().ok().and_then(|p| p.parent().map(Path::to_path_buf)).unwrap_or_else(|| PathBuf::from("."))
}

pub fn read_resource(name: &str) -> String {
    std::fs::read_to_string(exe_dir().join(name)).map(|s| s.trim().to_string()).unwrap_or_default()
}

fn valid_root(path: &Path) -> bool {
    path.join("desktron").join("lib").join("desk_launcher.py").is_file()
}

/// 产品根定位顺序与 macOS 壳一致：DESKTRON_HOME → 规范家目录 → 构建时写入的 desktron_home → 当前目录。
pub fn repo_root() -> PathBuf {
    if let Some(value) = std::env::var_os("DESKTRON_HOME") {
        let path = PathBuf::from(value);
        if valid_root(&path) {
            return path;
        }
    }
    let canonical = share().join("home");
    if valid_root(&canonical) {
        return canonical;
    }
    let baked = read_resource("desktron_home");
    if !baked.is_empty() && valid_root(Path::new(&baked)) {
        return PathBuf::from(baked);
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

#[derive(serde::Deserialize)]
struct PythonSpec {
    version: String,
}

/// 托管 Python：DESKTRON_PYTHON(开发) → runtime/python-<版本>-windows-<架构>/python.exe。
pub fn python(root: &Path) -> Result<PathBuf, String> {
    if let Some(value) = std::env::var_os("DESKTRON_PYTHON") {
        let path = PathBuf::from(value);
        if path.is_file() {
            return Ok(path);
        }
    }
    let spec_path = root.join("desktron").join("lib").join("provision").join("python-runtime.json");
    let spec: PythonSpec = std::fs::read_to_string(&spec_path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .ok_or_else(|| format!("缺少运行时声明 {}", spec_path.display()))?;
    let arch = if cfg!(target_arch = "aarch64") { "arm64" } else { "x64" };
    let runtime = data_home()
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(share)
        .join("runtime")
        .join(format!("python-{}-windows-{}", spec.version, arch));
    let exe = runtime.join("python.exe");
    if exe.is_file() {
        Ok(exe)
    } else {
        Err(format!("未找到 Desk 托管 Python({})。请重新运行安装程序 install.ps1。", exe.display()))
    }
}
