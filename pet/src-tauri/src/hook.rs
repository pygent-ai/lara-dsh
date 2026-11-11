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

/// Three taps must land within this window measured from the FIRST tap.
const TAP_WINDOW_MS: u32 = 800;
const VK_SHIFT: u32 = 0x10;
const VK_LSHIFT: u32 = 0xA0;
const VK_RSHIFT: u32 = 0xA1;
const LLKHF_INJECTED: u32 = 0x10;

static TRIGGER: OnceLock<Sender<()>> = OnceLock::new();
static FIRST_TAP_MS: AtomicU32 = AtomicU32::new(0);
static TAPS: AtomicU32 = AtomicU32::new(0);
static ALLOW_INJECTED: AtomicBool = AtomicBool::new(false);

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
        let info = &*(lparam as *const KBDLLHOOKSTRUCT);
        let keydown = wparam as u32 == WM_KEYDOWN || wparam as u32 == WM_SYSKEYDOWN;
        let is_shift =
            info.vkCode == VK_SHIFT || info.vkCode == VK_LSHIFT || info.vkCode == VK_RSHIFT;
        if keydown && is_shift && (ALLOW_INJECTED.load(Ordering::Relaxed) || info.flags & LLKHF_INJECTED == 0)
        {
            let now = now_ms();
            let first = FIRST_TAP_MS.load(Ordering::Relaxed);
            if TAPS.load(Ordering::Relaxed) == 0 || now.wrapping_sub(first) > TAP_WINDOW_MS {
                TAPS.store(1, Ordering::Relaxed);
                FIRST_TAP_MS.store(now, Ordering::Relaxed);
                eprintln!("lara-pet: shift tap #1");
            } else {
                let taps = TAPS.fetch_add(1, Ordering::Relaxed) + 1;
                eprintln!("lara-pet: shift tap #{taps}");
                if taps >= 3 {
                    TAPS.store(0, Ordering::Relaxed);
                    eprintln!("lara-pet: triple-shift TRIGGERED");
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
