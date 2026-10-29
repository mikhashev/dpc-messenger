import { describe, expect, it } from 'vitest';
import { formatSpeed, formatTimeLeft, speedAndTimeLeft } from './transferSpeed';

describe('formatSpeed', () => {
    it('counts bytes, then whole kilobytes, then megabytes with one decimal', () => {
        expect(formatSpeed(0)).toBe('0 B/s');
        expect(formatSpeed(512)).toBe('512 B/s');
        expect(formatSpeed(1023)).toBe('1023 B/s');
        expect(formatSpeed(1024)).toBe('1 KB/s');
        expect(formatSpeed(8192)).toBe('8 KB/s');
        expect(formatSpeed(1024 * 1024 - 1)).toBe('1024 KB/s');
        expect(formatSpeed(1024 * 1024)).toBe('1.0 MB/s');
        expect(formatSpeed(1.1 * 1024 * 1024)).toBe('1.1 MB/s');
        expect(formatSpeed(250 * 1024 * 1024)).toBe('250.0 MB/s');
    });
});

describe('formatTimeLeft', () => {
    it('says seconds under a minute, minutes under an hour, hours and minutes beyond', () => {
        expect(formatTimeLeft(0)).toBe('~0 s');
        expect(formatTimeLeft(45)).toBe('~45 s');
        expect(formatTimeLeft(59)).toBe('~59 s');
        expect(formatTimeLeft(60)).toBe('~1 min');
        expect(formatTimeLeft(12 * 60 + 10)).toBe('~12 min');
        expect(formatTimeLeft(2 * 3600)).toBe('~2 h');
        expect(formatTimeLeft(2 * 3600 + 5 * 60)).toBe('~2 h 5 min');
        expect(formatTimeLeft(3599)).toBe('~1 h');
    });
});

describe('speedAndTimeLeft', () => {
    it('reads the approved wording', () => {
        expect(speedAndTimeLeft({ bytes_per_second: 1.1 * 1024 * 1024, seconds_left: 7200 })).toBe('1.1 MB/s · ~2 h left');
    });

    it('shows nothing while the time left is not known', () => {
        expect(speedAndTimeLeft({ bytes_per_second: 0, seconds_left: null })).toBe('');
        expect(speedAndTimeLeft({})).toBe('');
        expect(speedAndTimeLeft({ bytes_per_second: 5000 })).toBe('');
    });
});
