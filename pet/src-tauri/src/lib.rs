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

// —— pet-config（热键 / 皮肤）持久化于 ~/.lara/pet-config.json ————————

fn pet_config_path() -> PathBuf {
    let home = std::env::var("USERPROFILE")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."));
    home.join(".lara").join("pet-config.json")
}

fn read_pet_config() -> serde_json::Value {
    std::fs::read_to_string(pet_config_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_else(|| {
            json!({
                "hotkey": {"key": "shift", "taps": 3, "window_ms": 800},
                "skin": "default"
            })
        })
}

fn apply_hotkey_from_config(config: &serde_json::Value) {
    let hotkey = &config["hotkey"];
    let key = hotkey["key"].as_str().unwrap_or("shift");
    let taps = hotkey["taps"].as_u64().unwrap_or(3) as u32;
    let window_ms = hotkey["window_ms"].as_u64().unwrap_or(800) as u32;
    hook::set_hotkey(key, taps, window_ms);
}

#[tauri::command]
fn get_pet_config() -> serde_json::Value {
    read_pet_config()
}

#[tauri::command]
fn set_pet_config(patch: serde_json::Value) -> Result<(), String> {
    let mut config = read_pet_config();
    if let Some(hotkey) = patch.get("hotkey") {
        config["hotkey"] = hotkey.clone();
        apply_hotkey_from_config(&config);
    }
    if let Some(skin) = patch.get("skin").and_then(|s| s.as_str()) {
        config["skin"] = json!(skin);
    }
    let path = pet_config_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    std::fs::write(&path, serde_json::to_string_pretty(&config).map_err(|e| e.to_string())?)
        .map_err(|e| format!("config write failed: {e}"))
}

/// Skin pack names available under pet/ui/skins (a dir with skin.json).
#[tauri::command]
fn list_skins() -> Vec<String> {
    let mut skins = Vec::new();
    if let Some(repo) = find_repo_dir() {
        if let Ok(entries) = std::fs::read_dir(repo.join("pet/ui/skins")) {
            for entry in entries.flatten() {
                if entry.path().join("skin.json").is_file() {
                    if let Some(name) = entry.file_name().to_str() {
                        skins.push(name.to_string());
                    }
                }
            }
        }
    }
    skins.sort();
    skins
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

/// Cursor polling: click-through outside the window, interactive inside.
/// Transparent corners still catch clicks while the cursor is inside the
/// rect; a full per-pixel hit-test needs the M3 cursor-forwarding design.
fn spawn_click_through(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let mut ignoring = false;
        loop {
            std::thread::sleep(std::time::Duration::from_millis(120));
            let Some(win) = app.get_webview_window("pet") else { continue };
            let (Ok(pos), Ok(size)) = (win.outer_position(), win.outer_size()) else {
                continue;
            };
            let mut point = POINT { x: 0, y: 0 };
            if unsafe { GetCursorPos(&mut point) } == 0 {
                continue;
            }
            let inside = point.x >= pos.x
                && point.x < pos.x + size.width as i32
                && point.y >= pos.y
                && point.y < pos.y + size.height as i32;
            let want = !inside;
            if want != ignoring {
                if win.set_ignore_cursor_events(want).is_ok() {
                    ignoring = want;
                }
            }
        }
    });
}

use windows_sys::Win32::Foundation::POINT;
use windows_sys::Win32::UI::WindowsAndMessaging::GetCursorPos;

pub fn run() {
    tauri::Builder::default()
        .manage(Sidecar::new())
        .setup(|app| {
            apply_hotkey_from_config(&read_pet_config());

            let handle = app.handle().clone();
            let (tx, rx) = mpsc::channel::<()>();
            hook::set_trigger(tx);
            std::thread::spawn(move || {
                for () in rx {
                    let _ = handle.emit("pet://triple-shift", ());
                }
            });
            std::thread::spawn(hook::install);
            spawn_click_through(app.handle().clone());

            // Tray: left-click toggles visibility; menu covers toggle + quit.
            let toggle = tauri::menu::MenuItem::with_id(app, "toggle", "显示 / 隐藏", true, None::<&str>)?;
            let quit = tauri::menu::MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = tauri::menu::Menu::with_items(app, &[&toggle, &quit])?;
            let tray = tauri::tray::TrayIconBuilder::new()
                .icon(app.default_window_icon().expect("bundle icon").clone())
                .menu(&menu)
                .show_menu_on_left_click(false)
                .build(app)?;
            tray.on_menu_event(|app, event| match event.id.as_ref() {
                "toggle" => toggle_pet_window(app),
                "quit" => app.exit(0),
                _ => {}
            });
            tray.on_tray_icon_event(|tray, event| {
                if let tauri::tray::TrayIconEvent::Click {
                    button: tauri::tray::MouseButton::Left,
                    button_state: tauri::tray::MouseButtonState::Up,
                    ..
                } = event
                {
                    toggle_pet_window(tray.app_handle());
                }
            });

            spawn_sidecar(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            quit_app,
            debug_fire_hotkey,
            agent_send,
            sidecar_status,
            get_pet_config,
            set_pet_config,
            list_skins
        ])
        .build(tauri::generate_context!())
        .expect("error while building lara pet")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                kill_sidecar(app_handle);
            }
        });
}

fn toggle_pet_window(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("pet") {
        if win.is_visible().unwrap_or(false) {
            let _ = win.hide();
        } else {
            let _ = win.show();
        }
    }
}
