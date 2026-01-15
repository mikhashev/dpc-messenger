// Audio recording module for Tauri - cross-platform voice recording
// Records to WAV format (16-bit PCM, 48 kHz mono)
// Python backend will transcode to OGG/Opus for transmission

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{Device, SampleFormat};
use std::io::{self, Write, BufWriter};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use std::sync::mpsc::{self, Receiver, Sender, RecvTimeoutError};
use std::thread;
use byteorder::{LittleEndian, WriteBytesExt};

// Recording configuration matching Telegram voice messages
const TELEGRAM_SAMPLE_RATE: u32 = 48000;  // 48 kHz (Telegram standard)
const TELEGRAM_CHANNELS: u8 = 1;          // Mono (voice doesn't need stereo)
const FRAME_SIZE_MS: u32 = 20;            // 20ms frames
const FRAME_SIZE_SAMPLES: usize = (TELEGRAM_SAMPLE_RATE as usize * FRAME_SIZE_MS as usize) / 1000; // 960 samples at 48kHz

// Audio samples sent from cpal callback to encoder thread
#[derive(Debug)]
enum AudioSample {
    Data(Vec<i16>),
    Stop,
}

// Recording state shared across commands
struct RecordingState {
    is_recording: bool,
    output_path: Option<PathBuf>,
    sample_rate: Option<u32>,
    channels: Option<u16>,
    sample_tx: Option<Sender<AudioSample>>,
}

impl RecordingState {
    fn new() -> Self {
        Self {
            is_recording: false,
            output_path: None,
            sample_rate: None,
            channels: None,
            sample_tx: None,
        }
    }
}

// Global recording state (using Arc<Mutex<>> for thread safety)
type GlobalState = Arc<Mutex<RecordingState>>;

fn get_global_state() -> GlobalState {
    use std::sync::OnceLock;
    static STATE: OnceLock<GlobalState> = OnceLock::new();
    STATE.get_or_init(|| Arc::new(Mutex::new(RecordingState::new())))
        .clone()
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

// WAV file writer (simple 16-bit PCM format)
struct WavWriter {
    file: BufWriter<std::fs::File>,
    data_size: u32,
}

impl WavWriter {
    fn new(path: &str, sample_rate: u32, channels: u16) -> io::Result<Self> {
        let file = std::fs::File::create(path)?;
        let mut file = BufWriter::with_capacity(64 * 1024, file);

        // Write RIFF header
        file.write_all(b"RIFF")?;
        // File size - 8 (will be updated on finish)
        file.write_u32::<LittleEndian>(0)?;
        // WAVE format
        file.write_all(b"WAVE")?;

        // fmt chunk
        file.write_all(b"fmt ")?;
        // Chunk size (16 for PCM)
        file.write_u32::<LittleEndian>(16)?;
        // Audio format (1 = PCM)
        file.write_u16::<LittleEndian>(1)?;
        // Channels
        file.write_u16::<LittleEndian>(channels)?;
        // Sample rate
        file.write_u32::<LittleEndian>(sample_rate)?;
        // Byte rate (sample_rate * channels * bits_per_sample / 8)
        let byte_rate = sample_rate * channels as u32 * 2;
        file.write_u32::<LittleEndian>(byte_rate)?;
        // Block align (channels * bits_per_sample / 8)
        file.write_u16::<LittleEndian>(channels * 2)?;
        // Bits per sample (16)
        file.write_u16::<LittleEndian>(16)?;

        // data chunk
        file.write_all(b"data")?;
        // Data size (will be updated on finish)
        file.write_u32::<LittleEndian>(0)?;

        Ok(Self {
            file,
            data_size: 0,
        })
    }

    fn write_samples(&mut self, samples: &[i16]) -> io::Result<()> {
        for &sample in samples {
            self.file.write_i16::<LittleEndian>(sample)?;
        }
        self.data_size += samples.len() as u32 * 2; // 2 bytes per sample
        Ok(())
    }

    fn finish(self) -> io::Result<()> {
        // Update data chunk size
        use std::io::Seek;
        let mut file = self.file.into_inner()?;

        // Data chunk size is at position 40 (after "data" marker)
        file.seek(std::io::SeekFrom::Start(40))?;
        file.write_u32::<LittleEndian>(self.data_size)?;

        // File size is at position 4
        let file_size = self.data_size + 36; // 36 = header size
        file.seek(std::io::SeekFrom::Start(4))?;
        file.write_u32::<LittleEndian>(file_size)?;

        file.flush()?;
        Ok(())
    }
}

/// Start audio recording to WAV format
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

    let device_config = device
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
    let file_path_str = file_path.to_string_lossy().to_string();

    // Store config for later use
    let sample_rate = TELEGRAM_SAMPLE_RATE;
    let channels = TELEGRAM_CHANNELS as u16;

    // Create channel for sending samples to encoder thread
    let (sample_tx, sample_rx) = mpsc::channel::<AudioSample>();

    // Spawn encoder thread
    let encoder_file_path = file_path_str.clone();
    let max_frames = (sample_rate as usize * max_duration_seconds as usize) / FRAME_SIZE_SAMPLES;

    thread::spawn(move || {
        encoder_thread(sample_rx, encoder_file_path, sample_rate, channels, max_frames);
    });

    // Start audio capture based on sample format
    let sample_tx_clone = sample_tx.clone();
    match device_config.sample_format() {
        SampleFormat::I16 => {
            start_audio_capture::<i16>(device, device_config, sample_rate, sample_tx_clone)?;
        }
        SampleFormat::F32 => {
            start_audio_capture::<f32>(device, device_config, sample_rate, sample_tx_clone)?;
        }
        _ => {
            let _ = sample_tx.send(AudioSample::Stop);
            return Err("Unsupported sample format".to_string());
        }
    }

    // Set recording state AFTER starting the stream
    state.is_recording = true;
    state.output_path = Some(file_path.clone());
    state.sample_rate = Some(sample_rate);
    state.channels = Some(channels);
    state.sample_tx = Some(sample_tx);

    Ok(RecordingStartResult {
        output_path: file_path_str,
        sample_rate,
        channels,
    })
}

/// Encoder thread - receives audio samples and writes WAV file
fn encoder_thread(
    sample_rx: Receiver<AudioSample>,
    output_path: String,
    sample_rate: u32,
    channels: u16,
    max_frames: usize,
) {
    // Create WAV writer
    let mut writer = WavWriter::new(&output_path, sample_rate, channels)
        .expect("Failed to create output file");

    // Buffer for accumulating samples
    let mut sample_buffer = Vec::new();
    let mut frames_written = 0usize;

    // Process samples until we receive Stop signal
    loop {
        match sample_rx.recv_timeout(Duration::from_millis(100)) {
            Ok(AudioSample::Data(mut samples)) => {
                sample_buffer.append(&mut samples);

                // Write complete frames
                while sample_buffer.len() >= FRAME_SIZE_SAMPLES {
                    if frames_written >= max_frames {
                        eprintln!("Max duration reached, stopping recording");
                        writer.finish().ok();
                        return;
                    }

                    let frame: Vec<i16> = sample_buffer.drain(..FRAME_SIZE_SAMPLES).collect();

                    // Write samples to WAV file
                    writer.write_samples(&frame)
                        .expect("Failed to write WAV data");

                    frames_written += 1;
                }
            }
            Ok(AudioSample::Stop) | Err(RecvTimeoutError::Disconnected) => {
                break;
            }
            Err(RecvTimeoutError::Timeout) => {
                // No data available, continue waiting
            }
        }
    }

    // Flush remaining samples (pad if needed)
    if !sample_buffer.is_empty() {
        // Pad to complete frame
        while sample_buffer.len() < FRAME_SIZE_SAMPLES {
            sample_buffer.push(0);
        }
        writer.write_samples(&sample_buffer).ok();
    }

    // Finalize WAV file
    writer.finish()
        .expect("Failed to finalize WAV file");

    println!("Encoder thread finalized: {} frames written", frames_written);
}

/// Start audio capture using cpal
fn start_audio_capture<T>(
    device: Device,
    device_config: cpal::SupportedStreamConfig,
    target_sample_rate: u32,
    sample_tx: Sender<AudioSample>,
) -> Result<(), String>
where
    T: cpal::Sample + cpal::SizedSample,
{
    let input_channels = device_config.channels() as usize;
    let device_sample_rate = device_config.sample_rate().0;
    let resample_ratio = target_sample_rate as f64 / device_sample_rate as f64;

    // Channel for sending samples from audio callback
    let (tx, rx) = mpsc::channel::<Vec<i16>>();

    // Spawn thread to process samples and send to encoder
    thread::spawn(move || {
        let mut input_buffer = Vec::new();
        let mut output_buffer = Vec::new();
        let mut src_idx = 0.0f64;

        loop {
            match rx.recv_timeout(Duration::from_millis(100)) {
                Ok(mut samples) => {
                    input_buffer.append(&mut samples);

                    // Resample to 48 kHz if needed
                    while input_buffer.len() >= 2 && (output_buffer.len() < FRAME_SIZE_SAMPLES * 2) {
                        src_idx += resample_ratio.recip();
                        let idx0 = src_idx.floor() as usize;
                        let idx1 = (idx0 + 1).min(input_buffer.len() - 1);
                        let frac = (src_idx.fract() * 1024.0) as i64;

                        if idx0 < input_buffer.len() {
                            let sample = ((input_buffer[idx0] as i64) * (1024 - frac)
                                         + (input_buffer[idx1] as i64) * frac) / 1024;
                            output_buffer.push(sample as i16);
                        }

                        if idx0 >= 1 {
                            input_buffer.drain(..1);
                            src_idx -= 1.0;
                        }
                    }

                    // Send complete frames to encoder
                    while output_buffer.len() >= FRAME_SIZE_SAMPLES {
                        let frame: Vec<i16> = output_buffer.drain(..FRAME_SIZE_SAMPLES).collect();
                        if sample_tx.send(AudioSample::Data(frame)).is_err() {
                            return;
                        }
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    break;
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    // Continue waiting
                }
            }
        }
    });

    // Setup cpal audio stream
    let err_callback = |err| {
        eprintln!("Audio input error: {}", err);
    };

    let tx_clone = tx.clone();
    let data_callback = move |data: &[T], _: &cpal::InputCallbackInfo| {
        let mut samples: Vec<i16> = Vec::with_capacity(data.len() / input_channels);

        for chunk in data.chunks(input_channels) {
            let mut sum: f32 = 0.0;
            for sample in chunk.iter() {
                let s_i16: i16;
                if std::mem::size_of::<T>() == std::mem::size_of::<i16>() {
                    s_i16 = i16::from_ne_bytes(unsafe {
                        std::mem::transmute_copy::<T, [u8; 2]>(sample)
                    });
                } else {
                    let s_f32: f32 = f32::from_ne_bytes(unsafe {
                        std::mem::transmute_copy::<T, [u8; 4]>(sample)
                    });
                    s_i16 = (s_f32.clamp(-1.0, 1.0) * 32767.0) as i16;
                }
                sum += s_i16 as f32;
            }
            let mono_sample = (sum / input_channels as f32) as i16;
            samples.push(mono_sample);
        }

        let _ = tx_clone.send(samples);
    };

    let stream_config = cpal::StreamConfig {
        channels: device_config.channels(),
        sample_rate: device_config.sample_rate(),
        buffer_size: cpal::BufferSize::Default,
    };

    let _stream = device
        .build_input_stream(&stream_config, data_callback, err_callback, None)
        .map_err(|e| format!("Failed to build input stream: {}", e))?;

    _stream.play()
        .map_err(|e| format!("Failed to play stream: {}", e))?;

    // Keep the stream alive
    std::mem::forget(_stream);

    Ok(())
}

/// Stop audio recording
pub fn stop_recording() -> Result<String, String> {
    let global_state = get_global_state();
    let mut state = global_state
        .lock()
        .map_err(|e| format!("Failed to acquire lock: {}", e))?;

    if !state.is_recording {
        return Err("Not recording".to_string());
    }

    state.is_recording = false;

    if let Some(tx) = &state.sample_tx {
        let _ = tx.send(AudioSample::Stop);
    }

    let output_path = state
        .output_path
        .as_ref()
        .ok_or("No recording in progress")?
        .to_string_lossy()
        .to_string();

    state.sample_tx = None;

    // Give the encoder thread time to finalize (WAV files finalize quickly)
    drop(state);
    std::thread::sleep(Duration::from_millis(500));

    // Verify the file exists
    use std::path::Path;
    let path = Path::new(&output_path);
    if !path.exists() {
        return Err(format!("Output file not found: {}", output_path));
    }

    let metadata = std::fs::metadata(&path)
        .map_err(|e| format!("Failed to get file metadata: {}", e))?;
    if metadata.len() < 100 {
        return Err(format!("Output file is too small ({} bytes): {}", metadata.len(), output_path));
    }

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
