/**
 * The speed and time-left text of an Active Transfers row, from the
 * `bytes_per_second` and `seconds_left` of `file_transfer_progress`
 * (managers/transfer_speed.py). Wording is Mike's, 2026-10-29:
 * `1.1 MB/s · ~2 h left`; nothing while `seconds_left` is null.
 */

const KB = 1024;
const MB = 1024 * 1024;

/** B/s and KB/s whole, MB/s with one decimal. */
export function formatSpeed(bytesPerSecond: number): string {
    const bps = Math.max(0, bytesPerSecond);
    if (bps < KB) return `${Math.round(bps)} B/s`;
    if (bps < MB) return `${Math.round(bps / KB)} KB/s`;
    return `${(bps / MB).toFixed(1)} MB/s`;
}

/** `~45 s`, `~12 min`, `~2 h 5 min` (`~2 h` on the hour). */
export function formatTimeLeft(seconds: number): string {
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `~${s} s`;
    const minutes = Math.round(s / 60);
    if (minutes < 60) return `~${minutes} min`;
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return m ? `~${h} h ${m} min` : `~${h} h`;
}

/** The row's text beside the percent, or '' while the speed is not known. */
export function speedAndTimeLeft(transfer: { bytes_per_second?: number | null; seconds_left?: number | null }): string {
    const left = transfer.seconds_left;
    if (left === null || left === undefined || !Number.isFinite(left)) return '';
    return `${formatSpeed(transfer.bytes_per_second ?? 0)} · ${formatTimeLeft(left)} left`;
}
