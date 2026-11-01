//! 后端 Supervisor：与 macOS `backend_supervisor.swift` 相同的状态机与宿主控制协议。
//!
//! - 原生壳是后端的唯一所有者。每次拉起分配新的 launch generation；只有 `/desk-probe`
//!   返回的 host_instance_id、launch_generation、pid 与 boot_id 全部匹配才算健康。
//! - 计划重启与异常退出走同一条串行状态机；连续失败按 0/1/2/4/8 秒退避，超过次数显式报错。
//! - 重启请求经 `~/.local/state/desktron/host-control/request-<op>.json` 提交，宿主在持有
//!   生命周期锁时认领并写回 accepted / completed / rejected / failed。
//! - Windows 没有 SIGTERM：正常停止经命名事件通知后端，4 秒后结束整个作业。
//! - Windows 版尚未接入应用内更新：update-request 一律以 update_launcher_unavailable 拒绝。

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::http;
use crate::paths;
use crate::winutil::{self, FileLock, Job, StopEvent};

pub const PORT: u16 = 7765;

#[derive(Clone, Debug, PartialEq)]
pub enum Phase {
    Idle,
    Launching(String),
    Healthy(String, String),
    Stopping(String),
    Backoff(usize),
    ForeignPort(Option<u32>),
    Degraded(String),
    Failed(String),
    Quitting,
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct Probe {
    #[serde(default)]
    pub desk: bool,
    pub host_instance_id: Option<String>,
    pub launch_generation: Option<String>,
    pub boot_id: Option<String>,
    pub pid: Option<u32>,
}

pub enum SupervisorEvent {
    Phase(Phase),
    Healthy(Probe),
}

#[derive(Clone, Debug, Deserialize)]
struct RestartRequest {
    operation_id: String,
    host_instance_id: String,
    launch_generation: String,
    boot_id: String,
    pid: u32,
    token: String,
    expires_at: f64,
}

#[derive(Serialize)]
struct RestartResponse<'a> {
    operation_id: &'a str,
    state: &'a str,
    error: Option<&'a str>,
    host_instance_id: &'a str,
    request_launch_generation: &'a str,
    request_boot_id: &'a str,
    request_pid: u32,
    target_launch_generation: Option<&'a str>,
    target_boot_id: Option<&'a str>,
}

pub struct LaunchSpec {
    pub root: PathBuf,
    pub python: Result<PathBuf, String>,
    pub app_control_token: String,
    pub host_rev: String,
    pub host_build: String,
}

enum Command_ {
    Shutdown(Sender<()>),
    ChildExited(String),
}

/// 主线程持有的句柄。
pub struct SupervisorHandle {
    tx: Sender<Command_>,
    pub host_instance_id: String,
}

impl SupervisorHandle {
    /// 请求停止后端并等待完成(最多约 5 秒)。
    pub fn shutdown_blocking(&self) {
        let (reply, done) = mpsc::channel();
        if self.tx.send(Command_::Shutdown(reply)).is_ok() {
            let _ = done.recv_timeout(Duration::from_secs(6));
        }
    }
}

struct Running {
    child: Child,
    job: Job,
    stop: StopEvent,
}

#[derive(Clone, Copy, PartialEq)]
enum Task {
    TerminateGen,
    KillGen,
}

struct Supervisor {
    spec: LaunchSpec,
    host_instance_id: String,
    control_token: String,
    control_root: PathBuf,
    emit: Box<dyn Fn(SupervisorEvent) + Send>,
    tx: Sender<Command_>,
    delays: [u64; 5],
    startup_timeout: Duration,
    stable_healthy_for: Duration,
    phase: Phase,
    running: Option<Running>,
    generation: String,
    launched_at: Instant,
    healthy_since: Option<Instant>,
    last_probe_at: Option<Instant>,
    failures: usize,
    scheduled: Option<Instant>,
    timers: Vec<(Instant, Task, String)>,
    loaded_generation: String,
    foreign_port_checks: u32,
    current_boot_id: String,
    restart_operation: String,
    restart_request: Option<RestartRequest>,
    planned_exit_generation: String,
    terminating_generation: String,
    lifecycle_lock: Option<FileLock>,
    log_path: PathBuf,
}

pub fn start(spec: LaunchSpec, emit: Box<dyn Fn(SupervisorEvent) + Send>) -> SupervisorHandle {
    let (tx, rx) = mpsc::channel();
    let host_instance_id = uuid::Uuid::new_v4().to_string();
    let control_root = paths::control_root();
    let _ = fs::create_dir_all(&control_root);
    let supervisor = Supervisor {
        spec,
        host_instance_id: host_instance_id.clone(),
        control_token: uuid::Uuid::new_v4().to_string(),
        control_root,
        emit,
        tx: tx.clone(),
        delays: [0, 1, 2, 4, 8],
        startup_timeout: Duration::from_secs(30),
        stable_healthy_for: Duration::from_secs(30),
        phase: Phase::Idle,
        running: None,
        generation: String::new(),
        launched_at: Instant::now(),
        healthy_since: None,
        last_probe_at: None,
        failures: 0,
        scheduled: Some(Instant::now()),
        timers: Vec::new(),
        loaded_generation: String::new(),
        foreign_port_checks: 0,
        current_boot_id: String::new(),
        restart_operation: String::new(),
        restart_request: None,
        planned_exit_generation: String::new(),
        terminating_generation: String::new(),
        lifecycle_lock: None,
        log_path: paths::logs_dir().join("desktron-web.log"),
    };
    thread::Builder::new()
        .name("desk-backend-supervisor".into())
        .spawn(move || supervisor.run(rx))
        .expect("无法启动后端 Supervisor 线程");
    SupervisorHandle { tx, host_instance_id }
}

fn now_epoch() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

pub fn probe() -> Option<Probe> {
    let response = http::request(PORT, "GET", "/desk-probe", &[], None, Duration::from_millis(400)).ok()?;
    if response.status != 200 {
        return None;
    }
    serde_json::from_slice(&response.body).ok()
}

impl Supervisor {
    fn run(mut self, rx: Receiver<Command_>) {
        loop {
            let next = self.next_deadline();
            let wait = next.map(|t| t.saturating_duration_since(Instant::now())).unwrap_or(Duration::from_secs(3600));
            match rx.recv_timeout(wait) {
                Ok(Command_::Shutdown(reply)) => {
                    self.shutdown();
                    let _ = reply.send(());
                    return;
                }
                Ok(Command_::ChildExited(generation)) => self.child_exited(&generation),
                Err(RecvTimeoutError::Timeout) => self.fire_due(),
                Err(RecvTimeoutError::Disconnected) => return,
            }
        }
    }

    fn next_deadline(&self) -> Option<Instant> {
        let mut next = self.scheduled;
        for (at, _, _) in &self.timers {
            next = Some(next.map_or(*at, |n| n.min(*at)));
        }
        next
    }

    fn fire_due(&mut self) {
        let now = Instant::now();
        let mut due: Vec<(Task, String)> = Vec::new();
        self.timers.retain(|(at, task, generation)| {
            if *at <= now {
                due.push((*task, generation.clone()));
                false
            } else {
                true
            }
        });
        for (task, generation) in due {
            match task {
                Task::TerminateGen => {
                    if self.generation == generation {
                        if let Some(running) = &self.running {
                            running.stop.set();
                        }
                    }
                }
                Task::KillGen => {
                    if self.generation == generation {
                        if let Some(running) = &self.running {
                            running.job.terminate();
                        }
                    }
                }
            }
        }
        if let Some(at) = self.scheduled {
            if at <= now {
                self.scheduled = None;
                self.reconcile();
            }
        }
    }

    fn schedule(&mut self, delay: Duration) {
        self.scheduled = Some(Instant::now() + delay);
    }

    fn after(&mut self, delay: Duration, task: Task) {
        self.timers.push((Instant::now() + delay, task, self.generation.clone()));
    }

    fn transition(&mut self, next: Phase) {
        self.phase = next.clone();
        (self.emit)(SupervisorEvent::Phase(next));
    }

    fn reconcile(&mut self) {
        if self.phase == Phase::Quitting {
            return;
        }
        self.process_control_requests();
        if matches!(self.phase, Phase::Stopping(_) | Phase::Failed(_)) {
            return;
        }
        if self.running.is_some() {
            let exited = self.running.as_mut().map(|r| r.child.try_wait().ok().flatten().is_some()).unwrap_or(false);
            if exited {
                let generation = self.generation.clone();
                self.child_exited(&generation);
            } else {
                self.verify();
            }
            return;
        }
        if let Some(owner) = winutil::listening_pid(PORT) {
            self.transition(Phase::ForeignPort(Some(owner)));
            self.foreign_port_checks += 1;
            // 父进程已死的后端会经看护线程自行退出；等它收尾，绝不结束它。
            if self.foreign_port_checks <= 24 {
                self.schedule(Duration::from_millis(250));
            } else if !self.restart_operation.is_empty() {
                self.fail_restart("端口被非当前后端占用");
            }
            return;
        }
        self.foreign_port_checks = 0;
        self.launch();
    }

    fn launch(&mut self) {
        if self.phase == Phase::Quitting || self.running.is_some() {
            return;
        }
        let generation = uuid::Uuid::new_v4().to_string();
        match self.spawn(&generation) {
            Ok(running) => {
                let pid = running.child.id();
                self.running = Some(running);
                self.generation = generation.clone();
                self.launched_at = Instant::now();
                self.healthy_since = None;
                self.last_probe_at = None;
                self.transition(Phase::Launching(generation.clone()));
                self.watch_exit(pid, generation);
                self.schedule(Duration::from_millis(50));
            }
            Err(error) => self.failed_launch(&error),
        }
    }

    fn spawn(&mut self, generation: &str) -> Result<Running, String> {
        let python = self.spec.python.clone()?;
        let root = &self.spec.root;
        let stop = StopEvent::new(format!("Local\\desktron-stop-{}", uuid::Uuid::new_v4()))?;
        let job = Job::new()?;
        let _ = fs::create_dir_all(paths::logs_dir());
        // 每次启动从干净日志开始，避免把上一轮的错误误认成本轮错误。
        let log = fs::File::create(&self.log_path).map_err(|e| e.to_string())?;
        let stamp = now_epoch();
        use std::io::Write;
        let mut header = log.try_clone().map_err(|e| e.to_string())?;
        let _ = writeln!(header, "[startup] native launch {stamp:.3}");
        let mut command = Command::new(python);
        command
            .arg(root.join("desktron").join("lib").join("desk_launcher.py"))
            .arg("ui")
            .arg(PORT.to_string())
            .current_dir(root)
            .env("DESKTRON_NATIVE_LAUNCH", "1")
            .env("DESKTRON_NATIVE_HOST_PID", std::process::id().to_string())
            .env("DESKTRON_HOST_INSTANCE_ID", &self.host_instance_id)
            .env("DESKTRON_LAUNCH_GENERATION", generation)
            .env("DESKTRON_HOST_CONTROL_TOKEN", &self.control_token)
            .env("DESKTRON_HOST_REV", &self.spec.host_rev)
            .env("DESKTRON_HOST_BUILD", &self.spec.host_build)
            .env("DESKTRON_HOST_STOP_EVENT", &stop.name)
            // 只有原生宿主持有。委托、审批与回写端点缺它一律 fail closed；不注入主页面 JS。
            .env("DESKTRON_APP_CONTROL_HOST_TOKEN", &self.spec.app_control_token)
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONUNBUFFERED", "1")
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log);
        let child = winutil::spawn_in_job(command, &job)?;
        winutil::inhibit_sleep(true);
        Ok(Running { child, job, stop })
    }

    fn watch_exit(&self, pid: u32, generation: String) {
        let tx = self.tx.clone();
        thread::spawn(move || {
            // 精确等待本代子进程；轮询存活状态，PID 被复用前进程句柄由 Running 持有。
            while winutil::process_alive(pid) {
                thread::sleep(Duration::from_millis(100));
            }
            let _ = tx.send(Command_::ChildExited(generation));
        });
    }

    fn verify(&mut self) {
        if self.phase == Phase::Quitting {
            return;
        }
        let expected = self.generation.clone();
        let pid = match &self.running {
            Some(running) => running.child.id(),
            None => return,
        };
        if let Some(probe) = probe() {
            let boot = probe.boot_id.clone().unwrap_or_default();
            let owned = probe.desk
                && probe.host_instance_id.as_deref() == Some(self.host_instance_id.as_str())
                && probe.launch_generation.as_deref() == Some(expected.as_str())
                && probe.pid == Some(pid)
                && !boot.is_empty();
            if owned {
                let now = Instant::now();
                let since = *self.healthy_since.get_or_insert(now);
                self.last_probe_at = Some(now);
                if now.duration_since(since) >= self.stable_healthy_for {
                    self.failures = 0;
                }
                self.current_boot_id = boot.clone();
                self.transition(Phase::Healthy(expected.clone(), boot.clone()));
                if !self.restart_operation.is_empty() {
                    if let Some(request) = self.restart_request.clone() {
                        if self.write_response(&request, "completed", None, Some(&expected), Some(&boot), false) {
                            self.restart_operation.clear();
                            self.restart_request = None;
                            self.lifecycle_lock = None;
                        }
                    }
                }
                if self.loaded_generation != expected {
                    self.loaded_generation = expected.clone();
                    (self.emit)(SupervisorEvent::Healthy(probe));
                }
                self.schedule(Duration::from_millis(500));
                return;
            }
            // 已证明身份的后端不因探活暂时异常而被重启；只有真实退出才驱动恢复。
            if self.last_probe_at.is_some() {
                self.transition(Phase::Degraded("后端探活身份暂时不匹配".into()));
                self.schedule(Duration::from_secs(1));
            } else {
                self.bounded_stop(&expected);
            }
            return;
        }
        if self.last_probe_at.is_some() {
            self.transition(Phase::Degraded("后端探活暂时不可用".into()));
            self.schedule(Duration::from_secs(1));
            return;
        }
        if Instant::now() >= self.launched_at + self.startup_timeout {
            self.bounded_stop(&expected);
            return;
        }
        self.schedule(Duration::from_millis(100));
    }

    fn child_exited(&mut self, generation: &str) {
        if self.phase == Phase::Quitting || self.generation != generation || self.running.is_none() {
            return;
        }
        if let Some(mut running) = self.running.take() {
            let _ = running.child.wait();
            running.job.terminate();
        }
        winutil::inhibit_sleep(false);
        self.generation.clear();
        self.current_boot_id.clear();
        self.terminating_generation.clear();
        if !self.restart_operation.is_empty() && self.planned_exit_generation == generation {
            self.planned_exit_generation.clear();
            self.transition(Phase::Idle);
            self.schedule(Duration::from_millis(0));
        } else {
            self.failed_launch("后端进程已退出");
        }
    }

    fn bounded_stop(&mut self, generation: &str) {
        if self.generation != generation || self.terminating_generation == generation {
            return;
        }
        self.terminating_generation = generation.to_string();
        if let Some(running) = &self.running {
            running.stop.set();
        }
        self.after(Duration::from_secs(4), Task::KillGen);
        let next = (self.failures + 1).max(1);
        self.transition(Phase::Backoff(next));
    }

    fn fail_restart(&mut self, reason: &str) {
        self.transition(Phase::Failed(reason.to_string()));
        if let Some(request) = self.restart_request.clone() {
            self.write_response(&request, "failed", Some(reason), None, None, false);
        }
        self.restart_operation.clear();
        self.restart_request = None;
        self.planned_exit_generation.clear();
        self.lifecycle_lock = None;
    }

    fn failed_launch(&mut self, reason: &str) {
        if self.phase == Phase::Quitting {
            return;
        }
        self.failures += 1;
        if self.failures > self.delays.len() {
            self.transition(Phase::Failed(reason.to_string()));
            if !self.restart_operation.is_empty() {
                self.fail_restart(reason);
            } else {
                self.lifecycle_lock = None;
            }
            return;
        }
        let delay = self.delays[self.failures - 1];
        self.transition(Phase::Backoff(self.failures));
        self.schedule(Duration::from_secs(delay));
    }

    fn shutdown(&mut self) {
        if self.phase == Phase::Quitting {
            return;
        }
        self.transition(Phase::Quitting);
        self.scheduled = None;
        self.timers.clear();
        if let Some(mut running) = self.running.take() {
            running.stop.set();
            if !winutil::wait_child(&running.child, 4000) {
                running.job.terminate();
                winutil::wait_child(&running.child, 1000);
            }
            let _ = running.child.wait();
        }
        winutil::inhibit_sleep(false);
        self.lifecycle_lock = None;
    }

    // ---------------------------------------------------------------- 宿主控制请求

    fn path(&self, prefix: &str, operation: &str) -> PathBuf {
        self.control_root.join(format!("{prefix}{operation}.json"))
    }

    fn acquire_lock(&mut self) -> bool {
        if self.lifecycle_lock.is_some() {
            return true;
        }
        self.lifecycle_lock = FileLock::try_acquire(&self.control_root.join("lifecycle.lock"));
        self.lifecycle_lock.is_some()
    }

    fn write_response(
        &self,
        request: &RestartRequest,
        state: &str,
        error: Option<&str>,
        target_generation: Option<&str>,
        target_boot: Option<&str>,
        update: bool,
    ) -> bool {
        let value = RestartResponse {
            operation_id: &request.operation_id,
            state,
            error,
            host_instance_id: &request.host_instance_id,
            request_launch_generation: &request.launch_generation,
            request_boot_id: &request.boot_id,
            request_pid: request.pid,
            target_launch_generation: target_generation,
            target_boot_id: target_boot,
        };
        let target = self.path(if update { "update-response-" } else { "response-" }, &request.operation_id);
        let Ok(data) = serde_json::to_vec(&value) else { return false };
        atomic_write(&target, &data)
    }

    fn owned(&self, request: &RestartRequest, pid: u32) -> bool {
        request.host_instance_id == self.host_instance_id
            && request.launch_generation == self.generation
            && request.boot_id == self.current_boot_id
            && request.pid == pid
            && request.token == self.control_token
    }

    fn process_control_requests(&mut self) {
        if !self.restart_operation.is_empty() || !matches!(self.phase, Phase::Healthy(_, _)) {
            return;
        }
        let pid = match &self.running {
            Some(running) => running.child.id(),
            None => return,
        };
        let names: BTreeSet<String> = fs::read_dir(&self.control_root)
            .map(|entries| {
                entries.filter_map(|e| e.ok()).map(|e| e.file_name().to_string_lossy().into_owned()).collect()
            })
            .unwrap_or_default();
        for name in names.iter().filter(|n| n.starts_with("update-request-") && n.ends_with(".json")) {
            let path = self.control_root.join(name);
            if !self.acquire_lock() {
                return;
            }
            let Some(request) = read_request(&path) else {
                self.lifecycle_lock = None;
                continue;
            };
            let (state, error) = if request.expires_at < now_epoch() {
                ("rejected", "request_expired")
            } else if !self.owned(&request, pid) {
                ("rejected", "identity_mismatch")
            } else {
                // Windows 版尚未接入原生更新器，明确拒绝，不执行替换。
                ("rejected", "update_launcher_unavailable")
            };
            if self.write_response(&request, state, Some(error), None, None, true) {
                let _ = fs::remove_file(&path);
            }
            self.lifecycle_lock = None;
        }
        for name in names.iter().filter(|n| n.starts_with("request-") && n.ends_with(".json")) {
            let path = self.control_root.join(name);
            if !self.acquire_lock() {
                return;
            }
            let Some(request) = read_request(&path) else {
                self.lifecycle_lock = None;
                continue;
            };
            if request.expires_at < now_epoch() {
                if self.write_response(&request, "rejected", Some("request_expired"), None, None, false) {
                    let _ = fs::remove_file(&path);
                }
                self.lifecycle_lock = None;
                continue;
            }
            if !self.owned(&request, pid) {
                if self.write_response(&request, "rejected", Some("identity_mismatch"), None, None, false) {
                    let _ = fs::remove_file(&path);
                }
                self.lifecycle_lock = None;
                continue;
            }
            if !self.write_response(&request, "accepted", None, None, None, false) {
                self.lifecycle_lock = None;
                continue;
            }
            self.restart_operation = request.operation_id.clone();
            self.planned_exit_generation = self.generation.clone();
            self.transition(Phase::Stopping(request.operation_id.clone()));
            self.restart_request = Some(request);
            let _ = fs::remove_file(&path);
            self.after(Duration::from_millis(150), Task::TerminateGen);
            self.after(Duration::from_secs(4), Task::KillGen);
            return;
        }
    }
}

fn read_request(path: &Path) -> Option<RestartRequest> {
    let text = fs::read_to_string(path).ok()?;
    let value: Value = serde_json::from_str(&text).ok()?;
    serde_json::from_value(value).ok()
}

/// 先写临时文件再替换；目标被读者短暂打开时有限重试。
pub fn atomic_write(target: &Path, data: &[u8]) -> bool {
    let temporary = target.with_extension(format!("tmp-{}", std::process::id()));
    if fs::write(&temporary, data).is_err() {
        return false;
    }
    for _ in 0..40 {
        if fs::rename(&temporary, target).is_ok() {
            return true;
        }
        thread::sleep(Duration::from_millis(25));
    }
    let _ = fs::remove_file(&temporary);
    false
}
