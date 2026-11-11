mod hook;

use std::sync::mpsc;

use tauri::{Emitter, Manager};

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

pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let handle = app.handle().clone();
            let (tx, rx) = mpsc::channel::<()>();
            hook::set_trigger(tx);
            std::thread::spawn(move || {
                for () in rx {
                    eprintln!("lara-pet: relaying pet://triple-shift to webview");
                    let _ = handle.emit("pet://triple-shift", ());
                }
            });
            std::thread::spawn(hook::install);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            set_click_through,
            quit_app,
            debug_fire_hotkey
        ])
        .run(tauri::generate_context!())
        .expect("error while running lara pet");
}
