/**
 * The music settings form's shape and its two pure helpers (config-parity card
 * THE-MUSIC-FILE-TRANSFER-VISION-AND-KNOWLEDGE-KEYS-HAVE-NO-CONTROL-IN-THE-UI, order 7):
 * the `[music]` keys the Create panel's engine form and the player's settings form
 * cover, the answer normalised into that shape whatever the command said, and the
 * payload of only the keys a save changed — a key left out keeps its stored value.
 *
 * The defaults and kinds mirror the backend readers, not guesses: the ports are
 * whole numbers (the form offers 1..65535), the idle figures numbers of at least 0,
 * `yue2_max_seq` a whole number of at least 0 (0 = the model context), the queue
 * limits at least 1, `yue2_backbone` stock or rs-tw, the devices CUDA0, Vulkan0,
 * MTL0, CPU or empty (the engine picks), and every path empty = its default folder.
 */

/** The `[music]` keys the two music forms cover, as the command answers them. */
export interface MusicSettings {
    // acestep.cpp — read at every start of that engine
    acestep_binary: string;
    acestep_models_dir: string;
    acestep_port: number;
    acestep_idle_stop_s: number;
    acestep_device: string;
    // yue2.cpp — read at every start of that engine
    yue2_binary: string;
    yue2_models_dir: string;
    yue2_backbone: string;
    yue2_port: number;
    yue2_idle_stop_s: number;
    yue2_adapters_dir: string;
    yue2_instrumental_adapter: string;
    yue2_device: string;
    yue2_max_seq: number;
    // the agent job limits — read at every propose
    agent_queue_max: number;
    turn_jobs_max: number;
    // the describer of a same-style song — read at every describe
    describe_alias: string;
    // live lyrics keys
    auto_align_lyrics: boolean;
    lrclib_enabled: boolean;
    // the player's keys
    export_dir: string;
    stream_ranges_linux: boolean;
    stream_port: number;
    poll_interval_s: number;
}

/** The backbones the reader knows (`music/job_schemas.py` YUE2_BACKBONES). */
export const YUE2_BACKBONES: readonly string[] = ['stock', 'rs-tw'];

/** The `--device` spellings the engines document; '' = the engine picks the best device. */
export const ENGINE_DEVICES: readonly string[] = ['', 'CUDA0', 'Vulkan0', 'MTL0', 'CPU'];

/** The reader's own defaults, shown while the form waits for the command's answer. */
export function emptyMusicSettings(): MusicSettings {
    return {
        acestep_binary: '', acestep_models_dir: '', acestep_port: 9995, acestep_idle_stop_s: 300,
        acestep_device: '',
        yue2_binary: '', yue2_models_dir: '', yue2_backbone: 'stock', yue2_port: 9994,
        yue2_idle_stop_s: 300, yue2_adapters_dir: '', yue2_instrumental_adapter: '',
        yue2_device: '', yue2_max_seq: 0,
        agent_queue_max: 2, turn_jobs_max: 1, describe_alias: 'auto',
        auto_align_lyrics: true, lrclib_enabled: true,
        export_dir: '', stream_ranges_linux: false, stream_port: 9996, poll_interval_s: 60,
    };
}

/** `value` as a finite number of at least `min`, else `fallback` — a number input must
 *  never hold NaN or a figure the writer would refuse. */
export function numberOr(value: unknown, min: number, fallback: number): number {
    const n = typeof value === 'number' ? value : Number(value);
    return Number.isFinite(n) && n >= min ? n : fallback;
}

/** The command's answer in the form's shape, whatever the backend said (an error path,
 *  an older build): numbers and booleans coerced, text trimmed, enums kept only when
 *  the reader knows them. */
export function normaliseMusicSettings(data: any): MusicSettings {
    const base = emptyMusicSettings();
    const text = (key: string, fallback: string): string =>
        typeof data?.[key] === 'string' ? data[key].trim() : fallback;
    const figure = (key: string, fallback: number, min: number): number =>
        numberOr(data?.[key], min, fallback);
    const flag = (key: string, fallback: boolean): boolean =>
        typeof data?.[key] === 'boolean' ? data[key] : fallback;
    const backbone = text('yue2_backbone', base.yue2_backbone);
    return {
        acestep_binary: text('acestep_binary', base.acestep_binary),
        acestep_models_dir: text('acestep_models_dir', base.acestep_models_dir),
        acestep_port: figure('acestep_port', base.acestep_port, 1),
        acestep_idle_stop_s: figure('acestep_idle_stop_s', base.acestep_idle_stop_s, 0),
        acestep_device: ENGINE_DEVICES.includes(text('acestep_device', '')) ? text('acestep_device', '') : '',
        yue2_binary: text('yue2_binary', base.yue2_binary),
        yue2_models_dir: text('yue2_models_dir', base.yue2_models_dir),
        yue2_backbone: YUE2_BACKBONES.includes(backbone) ? backbone : base.yue2_backbone,
        yue2_port: figure('yue2_port', base.yue2_port, 1),
        yue2_idle_stop_s: figure('yue2_idle_stop_s', base.yue2_idle_stop_s, 0),
        yue2_adapters_dir: text('yue2_adapters_dir', base.yue2_adapters_dir),
        yue2_instrumental_adapter: text('yue2_instrumental_adapter', base.yue2_instrumental_adapter),
        yue2_device: ENGINE_DEVICES.includes(text('yue2_device', '')) ? text('yue2_device', '') : '',
        yue2_max_seq: figure('yue2_max_seq', base.yue2_max_seq, 0),
        agent_queue_max: figure('agent_queue_max', base.agent_queue_max, 1),
        turn_jobs_max: figure('turn_jobs_max', base.turn_jobs_max, 1),
        describe_alias: text('describe_alias', base.describe_alias),
        auto_align_lyrics: flag('auto_align_lyrics', base.auto_align_lyrics),
        lrclib_enabled: flag('lrclib_enabled', base.lrclib_enabled),
        export_dir: text('export_dir', base.export_dir),
        stream_ranges_linux: flag('stream_ranges_linux', base.stream_ranges_linux),
        stream_port: figure('stream_port', base.stream_port, 1),
        poll_interval_s: figure('poll_interval_s', base.poll_interval_s, 0.1),
    };
}

/** Only the keys whose draft differs from what was loaded — a save leaves every other
 *  key's stored value alone, so two forms never rewrite each other's keys. */
export function musicSettingsPayload(loaded: MusicSettings, draft: MusicSettings): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(draft) as (keyof MusicSettings)[]) {
        if (loaded[key] !== draft[key]) out[key] = draft[key];
    }
    return out;
}
