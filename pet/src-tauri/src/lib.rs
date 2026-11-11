mod hook;

use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;

use serde_json::json;
use tauri::{Emitter, Manager};

/// The sidecar child process owning the pet's single realtime lara session.
struct Sidecar {
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
    error: Mutex<Option<String>>,
}

impl Sidecar {
    fn new() -> Self {
        Self {
            stdin: Mutex::new(None),
            child: Mutex::new(None),
            error: Mutex::new(None),
        }
    }
}

#[tauri::command]
fn set_click_through(window: tauri::WebviewWindow, on: bool) -> Result<(), String> {
    window
        .set_ignore_cursor_events(on)
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn quit_app(window: tauri::WebviewWindow) {
    window.app_handle().exit(0);
}

/// Debug only: fire the hotkey path bypassing the physical keyboard.
#[tauri::command]
fn debug_fire_hotkey() {
    hook::fire_for_debug();
}

/// Forward one user message to the sidecar's session lane.
#[tauri::command]
fn agent_send(state: tauri::State<Sidecar>, text: String) -> Result<(), String> {
    let mut guard = state.stdin.lock().map_err(|e| e.to_string())?;
    let stdin = guard.as_mut().ok_or_else(|| {
        state
            .error
            .lock()
            .map(|e| e.clone().unwrap_or_else(|| "sidecar is not running".into()))
            .unwrap_or_else(|_| "sidecar is not running".into())
    })?;
    let line = json!({ "type": "send", "text": text }).to_string();
    writeln!(stdin, "{line}").map_err(|e| format!("sidecar write failed: {e}"))?;
    stdin.flush().map_err(|e| format!("sidecar flush failed: {e}"))
}

#[tauri::command]
fn sidecar_status(state: tauri::State<Sidecar>) -> serde_json::Value {
    json!({
        "running": state.stdin.lock().map(|g| g.is_some()).unwrap_or(false),
        "error": state.error.lock().ok().and_then(|g| g.clone()),
    })
}

/// Locate the repo root by walking up from the exe (dev layout: pet/src-tauri/target/debug).
fn find_repo_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    exe.ancestors()
        .skip(1)
        .find(|dir| dir.join("pet/sidecar/main.py").is_file())
        .map(|dir| dir.to_path_buf())
}

fn spawn_sidecar(app: &tauri::AppHandle) {
    let state = app.state::<Sidecar>();
    let Some(repo) = find_repo_dir() else {
        *state.error.lock().unwrap() = Some("pet/sidecar/main.py not found".into());
        return;
    };
    let python = std::env::var("LARA_PET_PYTHON")
        .ok()
        .map(PathBuf::from)
        .or_else(|| {
            let venv = repo.join(".venv/Scripts/python.exe");
            venv.is_file().then_some(venv)
        })
        .unwrap_or_else(|| PathBuf::from("python"));

    let mut command = Command::new(python);
    command.arg(repo.join("pet/sidecar/main.py"));
    if let Ok(workspace) = std::env::var("LARA_PET_WORKSPACE") {
        command.arg("--workspace").arg(workspace);
    }
    let mut child = match command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
    {
        Ok(child) => child,
        Err(e) => {
            *state.error.lock().unwrap() = Some(format!("sidecar spawn failed: {e}"));
            return;
        }
    };
    let stdout = child.stdout.take().expect("piped stdout");
    let stdin = child.stdin.take().expect("piped stdin");

    let emitter = app.clone();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else { break };
            let payload = serde_json::from_str::<serde_json::Value>(&line)
                .unwrap_or_else(|_| json!({ "type": "log", "msg": line }));
            let _ = emitter.emit("pet://agent-event", payload);
        }
        let _ = emitter.emit("pet://agent-event", json!({ "type": "sidecar_exited" }));
    });

    *state.child.lock().unwrap() = Some(child);
    *state.stdin.lock().unwrap() = Some(stdin);
}

fn kill_sidecar(app: &tauri::AppHandle) {
    if let Ok(mut guard) = app.state::<Sidecar>().child.lock() {
        if let Some(child) = guard.as_mut() {
            let _ = child.kill();
        }
    }
}

pub fn run() {
    tauri::Builder::default()
        .manage(Sidecar::new())
        .setup(|app| {
            let handle = app.handle().clone();
            let (tx, rx) = mpsc::channel::<()>();
            hook::set_trigger(tx);
            std::thread::spawn(move || {
                for () in rx {
                    let _ = handle.emit("pet://triple-shift", ());
                }
            });
            std::thread::spawn(hook::install);
            spawn_sidecar(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            set_click_through,
            quit_app,
            debug_fire_hotkey,
            agent_send,
            sidecar_status
        ])
        .build(tauri::generate_context!())
        .expect("error while building lara pet")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                kill_sidecar(app_handle);
            }
        });
}
