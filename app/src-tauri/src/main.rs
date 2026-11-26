// Jarvis desktop app: a thin shell around the Core's pages.
//
// - Starts the Core and the voice service (unless they're already running) and
//   stops the ones it started when you quit.
// - Main window: the Core's chat page (chat, voice, approvals, memory).
//   Closing it hides it to the tray; Quit is in the tray menu.
// - Tray icon: shows whether the mic is on; click to open.
// - Hotkeys (work from any app):
//     F9 (hold)          talk to Jarvis (push-to-talk)
//     Ctrl+Shift+Space   quick-ask box over any app
//     Ctrl+Shift+M       mic on / off
// - Windows pop-ups for approval cards and replies while the window is hidden.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, File};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent, Wry};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
use tauri_plugin_notification::NotificationExt;

const CORE_PORT: u16 = 8765;
const VOICE_PORT: u16 = 8766;
const QUICK_URL: &str = "http://127.0.0.1:8765/quick";
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Services this app started (and so stops on quit).
struct Services(Mutex<Vec<(String, Child)>>);

struct TrayState {
    tray: TrayIcon<Wry>,
    mute_item: MenuItem<Wry>,
}

fn repo_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("JARVIS_REPO") {
        return PathBuf::from(dir);
    }
    // Built from C:\Jarvis\app\src-tauri: the repo is two levels up.
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..")
}

fn data_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("JARVIS_DATA_DIR") {
        return PathBuf::from(dir);
    }
    PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".into())).join("Jarvis")
}

fn port_open(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok()
}

/// Start `python -m <module>` from the repo's venv, hidden, logging to the data folder.
fn start_service(module: &str, port: u16) -> Option<Child> {
    if port_open(port) {
        return None; // already running (e.g. started from a terminal)
    }
    use std::os::windows::process::CommandExt;
    let repo = repo_dir();
    let python = repo.join(".venv").join("Scripts").join("python.exe");
    let logs = data_dir().join("logs");
    let _ = fs::create_dir_all(&logs);
    let log = File::create(logs.join(format!("{module}.log"))).ok()?;
    let err = log.try_clone().ok()?;
    match Command::new(python)
        .args(["-m", module])
        .current_dir(&repo)
        .stdin(Stdio::null())
        .stdout(log)
        .stderr(err)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
    {
        Ok(child) => Some(child),
        Err(e) => {
            eprintln!("could not start {module}: {e}");
            None
        }
    }
}

fn stop_services(app: &AppHandle) {
    let services = app.state::<Services>();
    let mut children = services.0.lock().unwrap();
    for (_, child) in children.iter_mut() {
        // /T: also the Claude Code processes the Core started.
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        let _ = child.wait();
    }
    children.clear();
}

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn eval_main(app: &AppHandle, js: &str) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.eval(js);
    }
}

fn toggle_quick(app: &AppHandle) {
    let Some(w) = app.get_webview_window("quick") else { return };
    if w.is_visible().unwrap_or(false) {
        let _ = w.hide();
    } else {
        // If the Core wasn't up when the box was created, load it now.
        let _ = w.eval(&format!(
            "if (!window.jarvisQuick) location.replace('{QUICK_URL}'); else jarvisQuick.reset();"
        ));
        let _ = w.center();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

// ----- commands the Core's pages call -----

#[tauri::command]
fn notify(app: AppHandle, title: String, body: String) {
    let _ = app.notification().builder().title(title).body(body).show();
}

/// The chat page reports the mic: "off", "ptt" or "hands-free".
#[tauri::command]
fn mic_state(app: AppHandle, state: String) {
    let Some(t) = app.try_state::<TrayState>() else { return };
    let on = state != "off";
    let label = match state.as_str() {
        "ptt" => "push-to-talk (hold F9)",
        "hands-free" => "hands-free (listening)",
        _ => "off",
    };
    let icon = if on {
        Image::from_bytes(include_bytes!("../icons/tray-on.png"))
    } else {
        Image::from_bytes(include_bytes!("../icons/tray-off.png"))
    };
    if let Ok(icon) = icon {
        let _ = t.tray.set_icon(Some(icon));
    }
    let _ = t.tray.set_tooltip(Some(format!("Jarvis · mic {label}")));
    let _ = t.mute_item.set_text(if on { "Turn mic off" } else { "Turn mic on" });
}

#[tauri::command]
fn hide_quick(app: AppHandle) {
    if let Some(w) = app.get_webview_window("quick") {
        let _ = w.hide();
    }
}

fn main() {
    let talk = Shortcut::new(None, Code::F9);
    let ask = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::Space);
    let mute = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyM);

    let app = tauri::Builder::default()
        // A second launch just brings the running one forward.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .plugin(tauri_plugin_notification::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(move |app, shortcut, event| {
                    let pressed = event.state() == ShortcutState::Pressed;
                    if shortcut == &talk {
                        eval_main(app, &format!("window.jarvis && window.jarvis.ptt({pressed})"));
                    } else if pressed && shortcut == &ask {
                        toggle_quick(app);
                    } else if pressed && shortcut == &mute {
                        eval_main(app, "window.jarvis && window.jarvis.toggleMute()");
                    }
                })
                .build(),
        )
        .manage(Services(Mutex::new(Vec::new())))
        .invoke_handler(tauri::generate_handler![notify, mic_state, hide_quick])
        .on_window_event(|window, event| match event {
            // Closing the main window keeps Jarvis running in the tray.
            WindowEvent::CloseRequested { api, .. } if window.label() == "main" => {
                api.prevent_close();
                let _ = window.hide();
            }
            // The ask box goes away when you click elsewhere.
            WindowEvent::Focused(false) if window.label() == "quick" => {
                let _ = window.hide();
            }
            _ => {}
        })
        .setup(move |app| {
            // Services first: the window's loading page waits for the Core.
            {
                let services = app.state::<Services>();
                let mut children = services.0.lock().unwrap();
                if let Some(c) = start_service("core", CORE_PORT) {
                    children.push(("core".into(), c));
                }
                if let Some(c) = start_service("voice", VOICE_PORT) {
                    children.push(("voice".into(), c));
                }
            }

            let open = MenuItem::with_id(app, "open", "Open Jarvis", true, None::<&str>)?;
            let ask_item = MenuItem::with_id(app, "ask", "Ask (Ctrl+Shift+Space)", true, None::<&str>)?;
            let mute_item = MenuItem::with_id(app, "mute", "Turn mic on", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Jarvis", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&open, &ask_item, &mute_item, &sep, &quit])?;
            let tray = TrayIconBuilder::with_id("jarvis")
                .icon(Image::from_bytes(include_bytes!("../icons/tray-off.png"))?)
                .tooltip("Jarvis · mic off")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show_main(app),
                    "ask" => toggle_quick(app),
                    "mute" => eval_main(app, "window.jarvis && window.jarvis.toggleMute()"),
                    "quit" => {
                        stop_services(app);
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main(tray.app_handle());
                    }
                })
                .build(app)?;
            app.manage(TrayState { tray, mute_item });

            WebviewWindowBuilder::new(app, "quick", WebviewUrl::External(QUICK_URL.parse().unwrap()))
                .title("Ask Jarvis")
                .inner_size(640.0, 340.0)
                .decorations(false)
                .always_on_top(true)
                .skip_taskbar(true)
                .resizable(false)
                .center()
                .visible(false)
                .build()?;

            let shortcuts = app.global_shortcut();
            for s in [talk, ask, mute] {
                if let Err(e) = shortcuts.register(s) {
                    eprintln!("hotkey {s:?} is taken by another app: {e}");
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Jarvis");

    app.run(|app, event| {
        if let RunEvent::Exit = event {
            stop_services(app);
        }
    });
}
