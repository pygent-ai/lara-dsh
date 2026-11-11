//! Global low-level keyboard hook: triple-tap Shift detection.
//!
//! The hook swallows the second and third Shift keydowns of a detected
//! sequence, so apps (e.g. JetBrains IDEs bound to double-Shift search) never
//! see the burst. Any other keydown resets the sequence; single Shift presses
//! always pass through untouched.

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::mpsc::Sender;
use std::sync::OnceLock;

use windows_sys::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::SystemInformation::GetTickCount;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, DispatchMessageW, GetMessageW, SetWindowsHookExW, TranslateMessage,
    KBDLLHOOKSTRUCT, MSG, WH_KEYBOARD_LL, WM_KEYDOWN, WM_SYSKEYDOWN,
};

const VK_SHIFT: u32 = 0x10;
const VK_LSHIFT: u32 = 0xA0;
const VK_RSHIFT: u32 = 0xA1;
const LLKHF_INJECTED: u32 = 0x10;

/// The hotkey definition, mutable at runtime from the settings panel.
/// Stored in atomics because the hook callback runs on the low-level hook
/// path: a blocking RwLock read there risks the system's LL-hook timeout
/// silently disabling the callback after a few slow events.
#[derive(Clone, Copy)]
pub struct HotkeyConfig {
    /// Virtual keys that count as a tap (generic + left/right variants).
    pub vks: [u32; 3],
    /// Taps inside the window that trigger the hotkey.
    pub taps: u32,
    /// Sequence window measured from the FIRST tap (ms).
    pub window_ms: u32,
}

static TAP_VK0: AtomicU32 = AtomicU32::new(VK_SHIFT);
static TAP_VK1: AtomicU32 = AtomicU32::new(VK_LSHIFT);
static TAP_VK2: AtomicU32 = AtomicU32::new(VK_RSHIFT);
static TAP_COUNT: AtomicU32 = AtomicU32::new(3);
static TAP_WINDOW: AtomicU32 = AtomicU32::new(800);

/// Apply a hotkey binding from the settings panel (VK set by key name).
pub fn set_hotkey(key: &str, taps: u32, window_ms: u32) {
    let (vk0, vk1, vk2) = match key {
        "ctrl" => (0x11, 0xA2, 0xA3),
        "alt" => (0x12, 0xA4, 0xA5),
        "grave" => (0xC0, 0xC0, 0xC0),
        "capslock" => (0x14, 0x14, 0x14),
        _ => (VK_SHIFT, VK_LSHIFT, VK_RSHIFT),
    };
    TAP_VK0.store(vk0, Ordering::Relaxed);
    TAP_VK1.store(vk1, Ordering::Relaxed);
    TAP_VK2.store(vk2, Ordering::Relaxed);
    TAP_COUNT.store(taps.clamp(2, 5), Ordering::Relaxed);
    TAP_WINDOW.store(window_ms.clamp(200, 2000), Ordering::Relaxed);
}

static TRIGGER: OnceLock<Sender<()>> = OnceLock::new();
static FIRST_TAP_MS: AtomicU32 = AtomicU32::new(0);
static TAPS: AtomicU32 = AtomicU32::new(0);
static ALLOW_INJECTED: AtomicBool = AtomicBool::new(false);
static FIRST_EVENT_LOGGED: AtomicBool = AtomicBool::new(false);

pub fn set_trigger(sender: Sender<()>) {
    let _ = TRIGGER.set(sender);
}

pub fn fire_for_debug() {
    if let Some(sender) = TRIGGER.get() {
        let _ = sender.send(());
    }
}

fn now_ms() -> u32 {
    // GetTickCount wraps ~every 49 days; wrapping arithmetic handles it.
    unsafe { GetTickCount() }
}

unsafe extern "system" fn hook_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    if code >= 0 {
        if !FIRST_EVENT_LOGGED.swap(true, Ordering::Relaxed) {
            eprintln!("lara-pet: hook received its first keyboard event");
        }
        let info = &*(lparam as *const KBDLLHOOKSTRUCT);
        let keydown = wparam as u32 == WM_KEYDOWN || wparam as u32 == WM_SYSKEYDOWN;
        let is_tap_key = info.vkCode == TAP_VK0.load(Ordering::Relaxed)
            || info.vkCode == TAP_VK1.load(Ordering::Relaxed)
            || info.vkCode == TAP_VK2.load(Ordering::Relaxed);
        if keydown
            && is_tap_key
            && (ALLOW_INJECTED.load(Ordering::Relaxed) || info.flags & LLKHF_INJECTED == 0)
        {
            let window_ms = TAP_WINDOW.load(Ordering::Relaxed);
            let trigger_at = TAP_COUNT.load(Ordering::Relaxed);
            let now = now_ms();
            let first = FIRST_TAP_MS.load(Ordering::Relaxed);
            if TAPS.load(Ordering::Relaxed) == 0 || now.wrapping_sub(first) > window_ms {
                TAPS.store(1, Ordering::Relaxed);
                FIRST_TAP_MS.store(now, Ordering::Relaxed);
                eprintln!("lara-pet: hotkey tap #1");
            } else {
                let taps = TAPS.fetch_add(1, Ordering::Relaxed) + 1;
                eprintln!("lara-pet: hotkey tap #{taps}");
                if taps >= trigger_at {
                    TAPS.store(0, Ordering::Relaxed);
                    eprintln!("lara-pet: hotkey TRIGGERED");
                    if let Some(sender) = TRIGGER.get() {
                        let _ = sender.send(());
                    }
                    return 1;
                }
                return 1;
            }
        } else if keydown {
            TAPS.store(0, Ordering::Relaxed);
        }
    }
    CallNextHookEx(std::ptr::null_mut(), code, wparam, lparam)
}

/// Install the hook; must run on a thread that pumps messages.
pub fn install() {
    unsafe {
        // Diagnostic helper: LARA_PET_ALLOW_INJECTED=1 lets SendInput-style
        // synthetic keys drive the detector for automated testing.
        if std::env::var("LARA_PET_ALLOW_INJECTED").is_ok() {
            ALLOW_INJECTED.store(true, Ordering::Relaxed);
        }
        let hmod = GetModuleHandleW(std::ptr::null());
        let hook = SetWindowsHookExW(WH_KEYBOARD_LL, Some(hook_proc), hmod, 0);
        if hook.is_null() {
            eprintln!("lara-pet: keyboard hook install FAILED");
            return;
        }
        eprintln!("lara-pet: keyboard hook installed");
        let mut msg: MSG = std::mem::zeroed();
        while GetMessageW(&mut msg, std::ptr::null_mut(), 0, 0) > 0 {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        // No UnhookWindowsHookEx on the happy path: process exit tears it down.
    }
}
