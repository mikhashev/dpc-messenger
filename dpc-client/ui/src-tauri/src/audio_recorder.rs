// Audio recording module for Tauri - cross-platform voice recording
// Works on Linux where getUserMedia doesn't work in WebKitGTK

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, SampleFormat};
use std::fs::File;
use std::io::BufWriter;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

// Recording state shared across commands
struct RecordingState {
    is_recording: bool,
    output_path: Option<PathBuf>,
    sample_rate: Option<u32>,
    channels: Option<u16>,
}

// We don't store the Stream in the state because it doesn't need to be kept
// The stream runs independently and we just signal it to stop via is_recording flag

impl RecordingState {
    fn new() -> Self {
        Self {
            is_recording: false,
            output_path: None,
            sample_rate: None,
            channels: None,
        }
    }
}

// Global recording state (using Arc<Mutex<>> for thread safety)
type GlobalState = Arc<Mutex<RecordingState>>;

fn get_global_state() -> GlobalState {
    static mut STATE: Option<GlobalState> = None;
    unsafe {
        STATE.get_or_insert_with(|| Arc::new(Mutex::new(RecordingState::new())))
            .clone()
    }
}

#[derive(Debug, serde::Serialize)]
pub struct RecordingStartResult {
    pub output_path: String,
    pub sample_rate: u32,
    pub channels: u16,
}

#[derive(Debug, serde::Serialize)]
pub struct RecordingStatus {
    pub is_recording: bool,
    pub output_path: Option<String>,
}

/// Start audio recording
/// Returns the output path and audio configuration
pub fn start_recording(
    output_dir: String,
    max_duration_seconds: u64,
) -> Result<RecordingStartResult, String> {
    let global_state = get_global_state();
    let mut state = global_state
        .lock()
        .map_err(|e| format!("Failed to acquire lock: {}", e))?;

    if state.is_recording {
        return Err("Already recording".to_string());
    }

    // Get default audio input device
    let host = cpal::default_host();
    let device = host
        .default_input_device()
        .ok_or("No audio input device found")?;

    let config = device
        .default_input_config()
        .map_err(|e| format!("Failed to get default input config: {}", e))?;

    // Create temp file in output directory
    let output_path = PathBuf::from(output_dir);
    std::fs::create_dir_all(&output_path)
        .map_err(|e| format!("Failed to create output directory: {}", e))?;

    // Generate filename with timestamp
    let timestamp = format!("{}", std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs());
    let filename = format!("voice_{}.wav", timestamp);
    let file_path = output_path.join(&filename);

    // Store config for later use
    let sample_rate = config.sample_rate().0;
    let channels = config.config().channels;

    // Set recording state BEFORE starting the stream
    state.is_recording = true;
    state.output_path = Some(file_path.clone());
    state.sample_rate = Some(sample_rate);
    state.channels = Some(channels);
    drop(state); // Release lock before spawning thread

    // Start recording based on sample format
    match config.sample_format() {
        SampleFormat::I16 => {
            spawn_record_thread::<i16>(device, config.into(), file_path.clone(), max_duration_seconds);
        }
        SampleFormat::U16 => {
            spawn_record_thread::<u16>(device, config.into(), file_path.clone(), max_duration_seconds);
        }
        SampleFormat::F32 => {
            spawn_record_thread::<f32>(device, config.into(), file_path.clone(), max_duration_seconds);
        }
        _ => {
            let mut state = global_state.lock().unwrap();
            state.is_recording = false;
            return Err("Unsupported sample format".to_string());
        }
    }

    Ok(RecordingStartResult {
        output_path: file_path.to_string_lossy().to_string(),
        sample_rate,
        channels,
    })
}

/// Spawn a recording thread in the background
fn spawn_record_thread<T>(
    device: Device,
    config: cpal::StreamConfig,
    output_path: PathBuf,
    max_duration_seconds: u64,
) where
    T: cpal::Sample + cpal::SizedSample + Into<f32>,
{
    std::thread::spawn(move || {
        if let Err(e) = record_audio::<T>(&device, &config, output_path, max_duration_seconds) {
            eprintln!("Recording error: {}", e);
            // Reset recording state on error
            let global_state = get_global_state();
            let mut state = global_state.lock().unwrap();
            state.is_recording = false;
        }
    });
}

/// Record audio to WAV file
fn record_audio<T>(
    device: &Device,
    config: &cpal::StreamConfig,
    output_path: PathBuf,
    max_duration_seconds: u64,
) -> Result<(), String>
where
    T: cpal::Sample + cpal::SizedSample + Into<f32>,
{
    let sample_rate = config.sample_rate.0;
    let channels = config.channels as usize;

    // Create WAV file writer
    let file = File::create(&output_path)
        .map_err(|e| format!("Failed to create output file: {}", e))?;
    let writer = BufWriter::new(file);

    // WAV header
    let spec = hound::WavSpec {
        channels: channels as u16,
        sample_rate: sample_rate,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };

    let wav_writer = Arc::new(Mutex::new(
        hound::WavWriter::new(writer, spec)
            .map_err(|e| format!("Failed to create WAV writer: {}", e))?,
    ));

    // Buffer for audio data
    let buffer: Arc<Mutex<Vec<Vec<f32>>>> = Arc::new(Mutex::new(Vec::new()));
    let buffer_clone = buffer.clone();
    let wav_writer_clone = wav_writer.clone();

    // Build the audio stream
    let err_callback = |err| {
        eprintln!("Audio input error: {}", err);
    };

    let data_callback = move |data: &[T], _: &cpal::InputCallbackInfo| {
        // Convert samples to f32 and store
        let samples: Vec<f32> = data.iter().map(|&s| s.into()).collect();
        if let Ok(mut buf) = buffer_clone.lock() {
            buf.push(samples);
        }
    };

    let _stream = device
        .build_input_stream(config, data_callback, err_callback, None)
        .map_err(|e| format!("Failed to build input stream: {}", e))?;

    // Play the stream
    _stream
        .play()
        .map_err(|e| format!("Failed to play stream: {}", e))?;

    // Spawn a thread to write samples to file
    let buffer_write = buffer.clone();

    std::thread::spawn(move || {
        let mut samples_written = 0u64;
        let max_samples = sample_rate as u64 * max_duration_seconds * channels as u64;

        loop {
            std::thread::sleep(Duration::from_millis(100));

            let mut buffer = buffer_write.lock().unwrap();
            if !buffer.is_empty() {
                let mut writer = wav_writer_clone.lock().unwrap();
                let mut should_stop = false;
                for samples in buffer.drain(..) {
                    for sample in samples {
                        if samples_written >= max_samples {
                            should_stop = true;
                            break;
                        }

                        // Convert f32 to i16 for WAV
                        let sample_i16 = (sample.clamp(-1.0, 1.0) * 32768.0) as i16;
                        let _ = writer.write_sample(sample_i16);
                        samples_written += 1;
                    }
                    if should_stop {
                        break;
                    }
                }
                drop(writer); // Drop writer lock before potentially returning
                drop(buffer); // Drop buffer lock (Drain is now gone)

                if should_stop {
                    // Max duration reached - signal to stop recording
                    let global_state = get_global_state();
                    let mut state = global_state.lock().unwrap();
                    state.is_recording = false;
                    return;
                }
            }

            // Check if still recording
            let global_state = get_global_state();
            let state = global_state.lock().unwrap();
            if !state.is_recording {
                // Finalize the WAV file - drop state first
                drop(state);
                drop(wav_writer_clone.lock().unwrap()); // WavWriter::Drop calls finalize()
                break;
            }
        }
    });

    // Keep the stream alive for the duration of the recording
    // We'll check the state periodically
    loop {
        std::thread::sleep(Duration::from_millis(500));
        let global_state = get_global_state();
        let state = global_state.lock().unwrap();
        if !state.is_recording {
            break;
        }
    }

    Ok(())
}

/// Stop audio recording and return the final file path
pub fn stop_recording() -> Result<String, String> {
    let global_state = get_global_state();
    let mut state = global_state
        .lock()
        .map_err(|e| format!("Failed to acquire lock: {}", e))?;

    if !state.is_recording {
        return Err("Not recording".to_string());
    }

    state.is_recording = false;

    let output_path = state
        .output_path
        .as_ref()
        .ok_or("No recording in progress")?
        .to_string_lossy()
        .to_string();

    // Give the writer thread time to finish
    std::thread::sleep(Duration::from_millis(300));

    Ok(output_path)
}

/// Get current recording status
pub fn get_recording_status() -> RecordingStatus {
    let global_state = get_global_state();
    let state = global_state.lock().unwrap();
    RecordingStatus {
        is_recording: state.is_recording,
        output_path: state
            .output_path
            .as_ref()
            .map(|p| p.to_string_lossy().to_string()),
    }
}

// Tauri command wrappers

#[tauri::command]
pub fn tauri_start_recording(
    output_dir: String,
    max_duration_seconds: u64,
) -> Result<RecordingStartResult, String> {
    start_recording(output_dir, max_duration_seconds)
}

#[tauri::command]
pub fn tauri_stop_recording() -> Result<String, String> {
    stop_recording()
}

#[tauri::command]
pub fn tauri_get_recording_status() -> RecordingStatus {
    get_recording_status()
}
