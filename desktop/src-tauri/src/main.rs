#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    env,
    fs::{self, OpenOptions},
    io::Write,
    net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

const FRONTEND_HOST: &str = "127.0.0.1";
const FRONTEND_PORT: u16 = 3002;
const PYTHON_API_PORT: u16 = 3000;
const CHAT_URL: &str = "http://127.0.0.1:3002/chat";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(180);
const PYTHON_API_WAIT_TIMEOUT: Duration = Duration::from_secs(45);

#[derive(Clone, Default)]
struct BackendState {
    child: Arc<Mutex<Option<Child>>>,
}

fn main() {
    let backend_state = BackendState::default();
    let panic_state = backend_state.clone();
    std::panic::set_hook(Box::new(move |panic_info| {
        eprintln!("AoiTalk Desktop panicked: {panic_info}");
        cleanup_backend_child(&panic_state);
    }));

    tauri::Builder::default()
        .manage(backend_state)
        .setup(|app| {
            let state = app.state::<BackendState>().inner().clone();
            setup_aoitalk(app, &state).map_err(|message| {
                Box::<dyn std::error::Error>::from(std::io::Error::new(
                    std::io::ErrorKind::Other,
                    message,
                ))
            })?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::CloseRequested { .. }) {
                let state = window.app_handle().state::<BackendState>();
                cleanup_backend_child(state.inner());
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build AoiTalk Desktop")
        .run(|app_handle, event| {
            if matches!(
                event,
                tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
            ) {
                let state = app_handle.state::<BackendState>();
                cleanup_backend_child(state.inner());
            }
        });
}

fn setup_aoitalk(app: &mut tauri::App, state: &BackendState) -> Result<(), String> {
    let repo_root = resolve_repo_root()?;
    let log_path = repo_root.join("logs").join("desktop-tauri-backend.log");

    if is_port_open(FRONTEND_HOST, FRONTEND_PORT, Duration::from_millis(500)) {
        append_desktop_log(
            &log_path,
            "Detected an existing AoiTalk frontend on 127.0.0.1:3002; connecting without starting a backend child.",
        );
        open_main_window(app, &log_path)?;
        return Ok(());
    }

    let mut child = start_backend_child(&repo_root, &log_path)?;
    wait_for_frontend(&mut child, &log_path)?;
    wait_for_python_api(&log_path);

    match state.child.lock() {
        Ok(mut guard) => {
            *guard = Some(child);
        }
        Err(_) => {
            terminate_process_tree(&mut child);
            return Err("Failed to store backend process state.".to_string());
        }
    }

    open_main_window(app, &log_path)
}

fn resolve_repo_root() -> Result<PathBuf, String> {
    let root = match env::var_os("AOITALK_ROOT") {
        Some(value) => PathBuf::from(value),
        None => {
            let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
            manifest_dir
                .parent()
                .and_then(Path::parent)
                .ok_or_else(|| {
                    "Failed to resolve the AoiTalk repo root from desktop/src-tauri.".to_string()
                })?
                .to_path_buf()
        }
    };

    let root = root
        .canonicalize()
        .map_err(|error| format!("Failed to resolve AOITALK_ROOT ({root:?}): {error}"))?;

    for required in ["main.py", "frontend/package.json", "pyproject.toml"] {
        let path = root.join(required);
        if !path.is_file() {
            return Err(format!(
                "AoiTalk repo root validation failed: {required} was not found under {}.",
                root.display()
            ));
        }
    }

    Ok(root)
}

fn venv_python(repo_root: &Path) -> PathBuf {
    if cfg!(windows) {
        repo_root.join("venv").join("Scripts").join("python.exe")
    } else {
        repo_root.join("venv").join("bin").join("python")
    }
}

fn start_backend_child(repo_root: &Path, log_path: &Path) -> Result<Child, String> {
    let python = venv_python(repo_root);
    if !python.is_file() {
        let message = format!(
            "Python virtual environment was not found: {}. setup.bat / setup.sh を先に実行してください。",
            python.display()
        );
        append_desktop_log(log_path, &message);
        return Err(message);
    }

    sync_frontend_env(repo_root, log_path);

    let log_parent = log_path.parent().ok_or_else(|| {
        format!(
            "Failed to resolve parent directory for backend log path: {}",
            log_path.display()
        )
    })?;
    fs::create_dir_all(log_parent).map_err(|error| {
        format!(
            "Failed to create backend log directory {}: {error}",
            log_parent.display()
        )
    })?;

    append_desktop_log(
        log_path,
        "Starting AoiTalk backend for Tauri Desktop with AOITALK_DESKTOP=1.",
    );

    let stdout_file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
        .map_err(|error| format!("Failed to open {}: {error}", log_path.display()))?;
    let stderr_file = stdout_file
        .try_clone()
        .map_err(|error| format!("Failed to clone backend log handle: {error}"))?;

    let mut command = Command::new(&python);
    command
        .arg("main.py")
        .current_dir(repo_root)
        .env("AOITALK_DESKTOP", "1")
        .env("AOITALK_WEB_AUTO_OPEN", "false")
        .env("AOITALK_SKIP_CADDY", "true")
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file));

    configure_backend_command(&mut command);

    command.spawn().map_err(|error| {
        format!(
            "Failed to start AoiTalk backend with {} main.py: {error}",
            python.display()
        )
    })
}

fn sync_frontend_env(repo_root: &Path, log_path: &Path) {
    let source = repo_root.join(".env");
    let target = repo_root.join("frontend").join(".env");
    if !source.is_file() {
        append_desktop_log(
            log_path,
            "Root .env was not found; continuing without copying frontend/.env.",
        );
        return;
    }

    if let Err(error) = fs::copy(&source, &target) {
        append_desktop_log(
            log_path,
            &format!(
                "Failed to copy {} to {}: {error}",
                source.display(),
                target.display()
            ),
        );
    }
}

#[cfg(windows)]
fn configure_backend_command(command: &mut Command) {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x08000000;
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(unix)]
fn configure_backend_command(command: &mut Command) {
    use std::os::unix::process::CommandExt;
    unsafe {
        command.pre_exec(|| {
            if libc::setsid() == -1 {
                Err(std::io::Error::last_os_error())
            } else {
                Ok(())
            }
        });
    }
}

fn wait_for_frontend(child: &mut Child, log_path: &Path) -> Result<(), String> {
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    while Instant::now() < deadline {
        if is_port_open(FRONTEND_HOST, FRONTEND_PORT, Duration::from_millis(500)) {
            append_desktop_log(log_path, "AoiTalk frontend is listening on 127.0.0.1:3002.");
            return Ok(());
        }

        match child.try_wait() {
            Ok(Some(status)) => {
                return Err(format!(
                    "AoiTalk backend exited before the frontend became ready: {status}.\n{}",
                    read_log_tail(log_path, 4000)
                ));
            }
            Ok(None) => {}
            Err(error) => {
                return Err(format!(
                    "Failed to inspect AoiTalk backend process while waiting for 127.0.0.1:3002: {error}"
                ));
            }
        }

        thread::sleep(Duration::from_millis(500));
    }

    let message = format!(
        "Timed out waiting for AoiTalk frontend on 127.0.0.1:3002.\n{}",
        read_log_tail(log_path, 4000)
    );
    append_desktop_log(log_path, &message);
    Err(message)
}

fn wait_for_python_api(log_path: &Path) {
    let deadline = Instant::now() + PYTHON_API_WAIT_TIMEOUT;
    while Instant::now() < deadline {
        if is_port_open(FRONTEND_HOST, PYTHON_API_PORT, Duration::from_millis(500)) {
            append_desktop_log(
                log_path,
                "AoiTalk Python API is listening on 127.0.0.1:3000.",
            );
            return;
        }
        thread::sleep(Duration::from_millis(500));
    }

    append_desktop_log(
        log_path,
        "AoiTalk Python API did not listen on 127.0.0.1:3000 before the optional wait timeout; continuing because the Next.js frontend is ready.",
    );
}

fn open_main_window(app: &mut tauri::App, log_path: &Path) -> Result<(), String> {
    let url = CHAT_URL
        .parse()
        .map_err(|error| format!("Invalid AoiTalk desktop URL {CHAT_URL}: {error}"))?;
    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url))
        .title("AoiTalk Desktop")
        .inner_size(1280.0, 860.0)
        .min_inner_size(960.0, 640.0)
        .resizable(true)
        .visible(true)
        .focused(true)
        .build()
        .and_then(|window| {
            window.show()?;
            window.set_focus()?;
            Ok(window)
        })
        .map_err(|error| format!("Failed to open AoiTalk Desktop window: {error}"))?;
    append_desktop_log(
        log_path,
        "AoiTalk Desktop main window was created and shown.",
    );
    Ok(())
}

fn is_port_open(host: &str, port: u16, timeout: Duration) -> bool {
    let ip = match host {
        "127.0.0.1" => IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1)),
        _ => return false,
    };
    TcpStream::connect_timeout(&SocketAddr::new(ip, port), timeout).is_ok()
}

fn append_desktop_log(log_path: &Path, message: &str) {
    if let Some(parent) = log_path.parent() {
        let _ = fs::create_dir_all(parent);
    }

    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(log_path) {
        let _ = writeln!(file, "\n[aoitalk-desktop] {message}");
    }
}

fn read_log_tail(log_path: &Path, max_bytes: usize) -> String {
    match fs::read(log_path) {
        Ok(bytes) => {
            let start = bytes.len().saturating_sub(max_bytes);
            String::from_utf8_lossy(&bytes[start..]).into_owned()
        }
        Err(error) => format!("Failed to read {}: {error}", log_path.display()),
    }
}

fn cleanup_backend_child(state: &BackendState) {
    let child = match state.child.lock() {
        Ok(mut guard) => guard.take(),
        Err(_) => None,
    };

    if let Some(mut child) = child {
        terminate_process_tree(&mut child);
    }
}

#[cfg(windows)]
fn terminate_process_tree(child: &mut Child) {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return;
    }

    let pid = child.id().to_string();
    let _ = Command::new("taskkill")
        .args(["/PID", &pid, "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    let _ = child.wait();
}

#[cfg(unix)]
fn terminate_process_tree(child: &mut Child) {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return;
    }

    let pgid = child.id() as i32;
    unsafe {
        libc::killpg(pgid, libc::SIGTERM);
    }

    for _ in 0..50 {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        thread::sleep(Duration::from_millis(100));
    }

    unsafe {
        libc::killpg(pgid, libc::SIGKILL);
    }
    let _ = child.wait();
}
