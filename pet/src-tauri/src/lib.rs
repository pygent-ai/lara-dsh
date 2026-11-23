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

// —— pet-config（热键 / 皮肤 / 窗口位置）持久化于 <dsh home>/lara/pet-config.json ——
// Lara 的用户根在 dsh 的 home 里（$DSH_HOME，默认 ~/.dsh），与 lara_shell.user_lara_root 一致。

fn lara_root() -> PathBuf {
    let dsh_home = std::env::var("DSH_HOME")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            std::env::var("USERPROFILE")
                .map(PathBuf::from)
                .unwrap_or_else(|_| PathBuf::from("."))
                .join(".dsh")
        });
    dsh_home.join("lara")
}

fn pet_config_path() -> PathBuf {
    lara_root().join("pet-config.json")
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

fn window_position(value: &serde_json::Value) -> Option<(i32, i32)> {
    let x = value.get("x")?.as_i64()?;
    let y = value.get("y")?.as_i64()?;
    Some((i32::try_from(x).ok()?, i32::try_from(y).ok()?))
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
    if let Some(window) = patch.get("window") {
        let (x, y) = window_position(window)
            .ok_or_else(|| "window position must contain 32-bit integer x/y".to_string())?;
        config["window"] = json!({"x": x, "y": y});
    }
    let path = pet_config_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    std::fs::write(&path, serde_json::to_string_pretty(&config).map_err(|e| e.to_string())?)
        .map_err(|e| format!("config write failed: {e}"))
}

/// Locate the repo root by walking up from the exe (dev layout: pet/src-tauri/target/debug).
fn find_repo_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    exe.ancestors()
        .skip(1)
        .find(|dir| dir.join("pet/src-tauri/Cargo.toml").is_file() && dir.join("pyproject.toml").is_file())
        .map(|dir| dir.to_path_buf())
}

/// The sidecar command: the installed `lara-pet-sidecar` console script.
///
/// Resolution order: `LARA_PET_SIDECAR` (explicit executable), the one
/// shipped next to this pet (the npm package's `bin/` layout), the script on
/// `PATH` (installed wheel), then the development checkout's virtualenv.
fn sidecar_command() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var("LARA_PET_SIDECAR") {
        return Some(PathBuf::from(explicit));
    }
    let script = if cfg!(windows) { "lara-pet-sidecar.exe" } else { "lara-pet-sidecar" };
    if let Ok(exe) = std::env::current_exe() {
        let bundled = exe.parent()?.join(script);
        if bundled.is_file() {
            return Some(bundled);
        }
    }
    if let Some(found) = std::env::var_os("PATH").and_then(|path| {
        std::env::split_paths(&path).map(|dir| dir.join(script)).find(|p| p.is_file())
    }) {
        return Some(found);
    }
    let venv_bin = if cfg!(windows) { "Scripts" } else { "bin" };
    find_repo_dir()
        .map(|repo| repo.join(".venv").join(venv_bin).join(script))
        .filter(|p| p.is_file())
}

/// `<lara root>/pet/sidecar.log`, truncated on every pet start.
fn sidecar_log_file() -> Option<std::fs::File> {
    let dir = lara_root().join("pet");
    std::fs::create_dir_all(&dir).ok()?;
    std::fs::File::create(dir.join("sidecar.log")).ok()
}

fn spawn_sidecar(app: &tauri::AppHandle) {
    let state = app.state::<Sidecar>();
    let Some(sidecar) = sidecar_command() else {
        let message = "lara-pet-sidecar not found: install the lara wheel (uv sync) or set LARA_PET_SIDECAR";
        eprintln!("lara-pet: {message}");
        *state.error.lock().unwrap() = Some(message.into());
        return;
    };
    eprintln!("lara-pet: sidecar {}", sidecar.display());

    let mut command = Command::new(sidecar);
    if let Ok(workspace) = std::env::var("LARA_PET_WORKSPACE") {
        command.arg("--workspace").arg(workspace);
    }
    // The pet's agent drives dsh through `lara dsh ...` bash calls; when the
    // bundled CLI (npm package bin/) is next to this pet, put it on the
    // sidecar's PATH so those calls resolve without a Python wheel.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let dir = dir.to_string_lossy().to_string();
            let path = std::env::var("PATH").map(|existing| format!("{dir};{existing}")).unwrap_or(dir);
            command.env("PATH", path);
        }
    }
    // The sidecar is a console script; without this flag Windows opens a
    // console window for it next to the pet. Its stderr (the sidecar's own
    // log) has nowhere to go from a GUI process, so it is kept in a file.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let stderr = sidecar_log_file()
        .map(Stdio::from)
        .unwrap_or_else(Stdio::null);
    let mut child = match command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(stderr)
        .spawn()
    {
        Ok(child) => child,
        Err(e) => {
            eprintln!("lara-pet: sidecar spawn failed: {e}");
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

/// Latest webview hit-test answer for the click-through poller: cursor
/// position in CSS px relative to the window, whether that point carries
/// visible/interactive content, and when it was measured.
#[derive(Clone, Copy)]
struct CursorHitReport {
    x: f64,
    y: f64,
    solid: bool,
    at: std::time::Instant,
}

/// Shared slot the webview reports into (``report_hit_test``) and the poller
/// reads from.
#[derive(Default)]
struct CursorHit(Mutex<Option<CursorHitReport>>);

/// Last observed window move. The event loop persists it only after movement
/// has been quiet for a moment, so dragging does not write the config per pixel.
#[derive(Default)]
struct WindowPlacement(Mutex<Option<(i32, i32, std::time::Instant)>>);

fn persist_window_position(x: i32, y: i32) -> Result<(), String> {
    let mut config = read_pet_config();
    config["window"] = json!({"x": x, "y": y});
    let path = pet_config_path();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    std::fs::write(
        &path,
        serde_json::to_string_pretty(&config).map_err(|e| e.to_string())?,
    )
    .map_err(|e| format!("config write failed: {e}"))
}

fn default_window_position(window: &tauri::WebviewWindow) -> Option<(i32, i32)> {
    let monitor = window.primary_monitor().ok().flatten()?;
    let work_area = monitor.work_area();
    let window_size = window.outer_size().ok()?;
    let width = i32::try_from(window_size.width).ok()?;
    let height = i32::try_from(window_size.height).ok()?;
    let work_width = i32::try_from(work_area.size.width).ok()?;
    let work_height = i32::try_from(work_area.size.height).ok()?;
    Some((
        work_area.position.x + work_width - width - 32,
        work_area.position.y + work_height - height - 32,
    ))
}

/// Webview → poller: hit-test result at a window-relative CSS position.
#[tauri::command]
fn report_hit_test(state: tauri::State<CursorHit>, x: f64, y: f64, solid: bool) {
    if let Ok(mut guard) = state.0.lock() {
        *guard = Some(CursorHitReport {
            x,
            y,
            solid,
            at: std::time::Instant::now(),
        });
    }
}

/// Per-pixel click-through. The poller owns the ignore-cursor-events flag;
/// the webview (hit_test.js) is the hit-test oracle:
/// - cursor outside the window rect: always pass-through;
/// - inside: the webview's answer at the cursor position decides — visible or
///   interactive content receives events, blank (transparent) areas pass
///   clicks to the windows behind (the old rect test blocked those).
/// Pass-through-first while the answer is missing or stale: a freshly entered
/// spot may need one more poll tick to become interactive, but blank pixels
/// never capture clicks. Answers older than HIT_TTL are re-probed so DOM
/// changes under a stationary cursor (bubble opens/closes) also heal.
fn spawn_click_through(app: tauri::AppHandle) {
    const HIT_TTL: std::time::Duration = std::time::Duration::from_millis(500);
    std::thread::spawn(move || {
        let mut ignoring = true;
        loop {
            std::thread::sleep(std::time::Duration::from_millis(30));
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
            let mut want = true;
            if inside {
                let Ok(scale) = win.scale_factor() else { continue };
                let x = (point.x - pos.x) as f64 / scale;
                let y = (point.y - pos.y) as f64 / scale;
                let report = app
                    .state::<CursorHit>()
                    .0
                    .lock()
                    .ok()
                    .and_then(|hit| *hit)
                    .filter(|hit| {
                        hit.at.elapsed() < HIT_TTL
                            && (hit.x - x).abs() <= 2.0
                            && (hit.y - y).abs() <= 2.0
                    });
                match report {
                    Some(hit) => want = !hit.solid,
                    // No answer (yet/anymore) for this spot: keep the current
                    // state and ask the webview to measure again.
                    None => {
                        let _ = win.eval(&format!(
                            "window.__laraPet&&window.__laraPet.hitTestAt({x:.0},{y:.0})"
                        ));
                        want = ignoring;
                    }
                }
            }
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
        .manage(CursorHit::default())
        .manage(WindowPlacement::default())
        .setup(|app| {
            let config = read_pet_config();
            apply_hotkey_from_config(&config);

            if let Some(win) = app.get_webview_window("pet") {
                let saved = config.get("window").and_then(window_position);
                if let Some((x, y)) = saved.or_else(|| default_window_position(&win)) {
                    let _ = win.set_position(tauri::PhysicalPosition::new(x, y));
                    if saved.is_none() {
                        if let Err(error) = persist_window_position(x, y) {
                            eprintln!("lara-pet: failed to persist initial window position: {error}");
                        }
                    }
                }
            }

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
            // The poller assumes pass-through as the initial state; make the
            // window actually match (wry starts cursor-enabled), or the first
            // blank-area hover would agree with the stale local belief and
            // never flip the style.
            if let Some(win) = app.get_webview_window("pet") {
                let _ = win.set_ignore_cursor_events(true);
            }

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
            report_hit_test
        ])
        .build(tauri::generate_context!())
        .expect("error while building lara pet")
        .run(|app_handle, event| match event {
            tauri::RunEvent::WindowEvent { label, event, .. } if label == "pet" => {
                if let tauri::WindowEvent::Moved(position) = event {
                    if let Ok(mut pending) = app_handle.state::<WindowPlacement>().0.lock() {
                        *pending = Some((position.x, position.y, std::time::Instant::now()));
                    }
                }
            }
            tauri::RunEvent::MainEventsCleared => {
                let ready = app_handle
                    .state::<WindowPlacement>()
                    .0
                    .lock()
                    .ok()
                    .and_then(|mut pending| {
                        let (_, _, changed_at) = pending.as_ref()?;
                        if changed_at.elapsed() < std::time::Duration::from_millis(250) {
                            return None;
                        }
                        pending.take()
                    });
                if let Some((x, y, _)) = ready {
                    if let Err(error) = persist_window_position(x, y) {
                        eprintln!("lara-pet: failed to persist window position: {error}");
                    }
                }
            }
            tauri::RunEvent::Exit => {
                if let Ok(mut pending) = app_handle.state::<WindowPlacement>().0.lock() {
                    if let Some((x, y, _)) = pending.take() {
                        let _ = persist_window_position(x, y);
                    }
                }
                kill_sidecar(app_handle);
            }
            _ => {}
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

#[cfg(test)]
mod tests {
    use super::window_position;
    use serde_json::json;

    #[test]
    fn window_position_accepts_negative_physical_coordinates() {
        assert_eq!(
            window_position(&json!({"x": -1920, "y": 48})),
            Some((-1920, 48))
        );
    }

    #[test]
    fn window_position_rejects_missing_fractional_or_out_of_range_values() {
        assert_eq!(window_position(&json!({"x": 1})), None);
        assert_eq!(window_position(&json!({"x": 1.5, "y": 2})), None);
        assert_eq!(window_position(&json!({"x": i64::MAX, "y": 2})), None);
    }
}
