//! Windows 系统接口：进程、Job Object、文件锁、端口占用者、休眠抑制、命名事件。
//!
//! 与 Python 侧 `desktron/lib/oscompat.py` 保持同一约定：
//! - 生命周期锁锁定 `lifecycle.lock` 在 0x7FFFFFFF_00000000 处的 1 字节；
//! - 后端停止事件名经 `DESKTRON_HOST_STOP_EVENT` 传给后端。

use std::ffi::{c_void, OsStr};
use std::fs::{File, OpenOptions};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command, Stdio};

use windows::core::PCWSTR;
use windows::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS, HANDLE, WAIT_OBJECT_0};
use windows::Win32::NetworkManagement::IpHelper::{
    GetExtendedTcpTable, MIB_TCPROW_OWNER_PID, MIB_TCPTABLE_OWNER_PID, TCP_TABLE_OWNER_PID_LISTENER,
};
use windows::Win32::Networking::WinSock::AF_INET;
use windows::Win32::Storage::FileSystem::{
    LockFileEx, UnlockFileEx, LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY,
};
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
    TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_BREAKAWAY_OK,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows::Win32::System::Power::{SetThreadExecutionState, ES_CONTINUOUS, ES_SYSTEM_REQUIRED};
use windows::Win32::System::Threading::{
    CreateEventW, CreateMutexW, GetExitCodeProcess, OpenProcess, SetEvent, WaitForSingleObject,
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, PROCESS_QUERY_LIMITED_INFORMATION,
};
use windows::Win32::System::IO::OVERLAPPED;

pub fn wide(value: &str) -> Vec<u16> {
    OsStr::new(value).encode_wide().chain(std::iter::once(0)).collect()
}

/// 进程句柄或事件句柄的所有权封装；Drop 时关闭。
pub struct OwnedHandle(pub HANDLE);

unsafe impl Send for OwnedHandle {}
unsafe impl Sync for OwnedHandle {}

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        if !self.0.is_invalid() {
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }
}

/// KILL_ON_JOB_CLOSE 作业：句柄关闭(包括本进程崩溃)时结束作业内全部进程。
pub struct Job(OwnedHandle);

impl Job {
    pub fn new() -> Result<Job, String> {
        unsafe {
            let handle = CreateJobObjectW(None, PCWSTR::null()).map_err(|e| e.to_string())?;
            let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK;
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
            .map_err(|e| e.to_string())?;
            Ok(Job(OwnedHandle(handle)))
        }
    }

    pub fn assign(&self, child: &Child) -> Result<(), String> {
        unsafe { AssignProcessToJobObject(self.0 .0, HANDLE(child.as_raw_handle())).map_err(|e| e.to_string()) }
    }

    pub fn terminate(&self) {
        unsafe {
            let _ = TerminateJobObject(self.0 .0, 1);
        }
    }
}

/// 手动重置的命名事件，用于请求后端走正常退出路径。
pub struct StopEvent {
    pub name: String,
    handle: OwnedHandle,
}

impl StopEvent {
    pub fn new(name: String) -> Result<StopEvent, String> {
        let w = wide(&name);
        unsafe {
            let handle = CreateEventW(None, true, false, PCWSTR(w.as_ptr())).map_err(|e| e.to_string())?;
            Ok(StopEvent { name, handle: OwnedHandle(handle) })
        }
    }

    pub fn set(&self) {
        unsafe {
            let _ = SetEvent(self.handle.0);
        }
    }
}

/// 后端子进程：带停止事件与独立作业。
pub fn spawn_in_job(mut command: Command, job: &Job) -> Result<Child, String> {
    // GUI 进程没有控制台。CREATE_NO_WINDOW 给后端一个隐藏控制台，它的命令行子进程共用它，
    // 不会各自弹出黑窗；独立进程组避免控制台信号波及宿主。
    command.creation_flags((CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP).0);
    command.stdin(Stdio::null());
    let child = command.spawn().map_err(|e| e.to_string())?;
    if let Err(error) = job.assign(&child) {
        let mut child = child;
        let _ = child.kill();
        return Err(format!("无法把后端放入作业：{error}"));
    }
    Ok(child)
}

pub fn process_alive(pid: u32) -> bool {
    unsafe {
        match OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) {
            Ok(handle) => {
                let owned = OwnedHandle(handle);
                let mut code = 0u32;
                GetExitCodeProcess(owned.0, &mut code).is_ok() && code == 259
            }
            Err(_) => GetLastError().0 == 5,
        }
    }
}

/// 在 port 上监听的进程 PID(仅 IPv4)。
pub fn listening_pid(port: u16) -> Option<u32> {
    unsafe {
        let mut size = 0u32;
        let _ = GetExtendedTcpTable(None, &mut size, false, AF_INET.0 as u32, TCP_TABLE_OWNER_PID_LISTENER, 0);
        for _ in 0..4 {
            let mut buffer = vec![0u8; size as usize + 4096];
            size = buffer.len() as u32;
            let rc = GetExtendedTcpTable(
                Some(buffer.as_mut_ptr() as *mut c_void),
                &mut size,
                false,
                AF_INET.0 as u32,
                TCP_TABLE_OWNER_PID_LISTENER,
                0,
            );
            if rc != 0 {
                continue;
            }
            let table = &*(buffer.as_ptr() as *const MIB_TCPTABLE_OWNER_PID);
            let rows: &[MIB_TCPROW_OWNER_PID] =
                std::slice::from_raw_parts(table.table.as_ptr(), table.dwNumEntries as usize);
            for row in rows {
                let local = u16::from_be((row.dwLocalPort & 0xFFFF) as u16);
                if local == port {
                    return Some(row.dwOwningPid);
                }
            }
            return None;
        }
        None
    }
}

/// 生命周期锁：与 Python `oscompat.lock_file` 锁同一字节范围。
pub struct FileLock {
    file: File,
}

impl FileLock {
    pub fn try_acquire(path: &Path) -> Option<FileLock> {
        let file = OpenOptions::new().read(true).write(true).create(true).truncate(false).open(path).ok()?;
        let mut overlapped = OVERLAPPED::default();
        overlapped.Anonymous.Anonymous.Offset = 0;
        overlapped.Anonymous.Anonymous.OffsetHigh = 0x7FFF_FFFF;
        let ok = unsafe {
            LockFileEx(
                HANDLE(file.as_raw_handle()),
                LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                None,
                1,
                0,
                &mut overlapped,
            )
        };
        ok.ok().map(|_| FileLock { file })
    }
}

impl Drop for FileLock {
    fn drop(&mut self) {
        let mut overlapped = OVERLAPPED::default();
        overlapped.Anonymous.Anonymous.Offset = 0;
        overlapped.Anonymous.Anonymous.OffsetHigh = 0x7FFF_FFFF;
        unsafe {
            let _ = UnlockFileEx(HANDLE(self.file.as_raw_handle()), None, 1, 0, &mut overlapped);
        }
    }
}

/// 后端运行期间阻止系统因空闲睡眠(对应 macOS 的 caffeinate -is)。
pub fn inhibit_sleep(enabled: bool) {
    unsafe {
        let flags = if enabled { ES_CONTINUOUS | ES_SYSTEM_REQUIRED } else { ES_CONTINUOUS };
        SetThreadExecutionState(flags);
    }
}

/// 单实例：已有 Desktron 宿主在运行时返回 None。
pub fn single_instance() -> Option<OwnedHandle> {
    let name = wide("Local\\io.github.desktron.host");
    unsafe {
        let handle = CreateMutexW(None, false, PCWSTR(name.as_ptr())).ok()?;
        if GetLastError() == ERROR_ALREADY_EXISTS {
            let _ = CloseHandle(handle);
            return None;
        }
        Some(OwnedHandle(handle))
    }
}

/// 等待进程句柄退出，最多 timeout_ms。返回是否已退出。
pub fn wait_child(child: &Child, timeout_ms: u32) -> bool {
    unsafe { WaitForSingleObject(HANDLE(child.as_raw_handle()), timeout_ms) == WAIT_OBJECT_0 }
}

/// 用系统默认程序打开网址或协议链接。
pub fn shell_open(target: &str) -> bool {
    use windows::Win32::UI::Shell::ShellExecuteW;
    use windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
    let verb = wide("open");
    let file = wide(target);
    let result = unsafe {
        ShellExecuteW(None, PCWSTR(verb.as_ptr()), PCWSTR(file.as_ptr()), PCWSTR::null(), PCWSTR::null(), SW_SHOWNORMAL)
    };
    result.0 as isize > 32
}
