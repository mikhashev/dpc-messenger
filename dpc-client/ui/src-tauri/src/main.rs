// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// --- THE DEFINITIVE FIX ---
// Import the `Manager` trait to bring its methods into scope.
use tauri::Manager;

// File metadata helper for dynamic timeout calculation (v0.11.2+)
#[tauri::command]
fn get_file_metadata(path: String) -> Result<FileMetadata, String> {
    let metadata = std::fs::metadata(&path)
        .map_err(|e| format!("Failed to get file metadata: {}", e))?;

    Ok(FileMetadata {
        size: metadata.len(),
        is_file: metadata.is_file(),
    })
}

// Get home directory path (v0.15.0+)
#[tauri::command]
fn get_home_directory() -> Result<String, String> {
    Ok(std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| ".".to_string()))
}

#[derive(serde::Serialize)]
struct FileMetadata {
    size: u64,
    is_file: bool,
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_fs::init())
        .invoke_handler(tauri::generate_handler![get_file_metadata, get_home_directory])
        .setup(|app| {
            #[cfg(debug_assertions)]
            {
                // Now that the `Manager` trait is in scope, this call will work.
                if let Some(window) = app.get_webview_window("main") {
                    window.open_devtools();
                }
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}