import { describe, it, expect } from 'vitest';
import {
    LRCLIB_EGRESS_TEXT,
    LYRICS_OVERWRITE_CONFIRM_TEXT,
    TRANSCRIBE_NOTE_TEXT,
    TRANSCRIBE_OVERWRITE_CONFIRM_TEXT,
    countUnstampedLines,
    currentLyricIndex,
    formatLrc,
    guessArtistTitle,
    lrcStamp,
    lrclibLookupPayload,
    lrclibOutcomeText,
    lyricsBadge,
    lyricsIsOwnEdit,
    lyricsSynced,
    lyricsWindow,
    isCommandTimeout,
    runTranscribe,
    stampLrcLine,
    transcribeLyricsPayload,
    transcriptionText,
    type LibraryEntry,
    type Lyrics,
    type LyricsAnswer,
    type LyricsLine,
    type MusicAnswer,
} from './musicPlayer';
import lyricsPanel from '../components/music/MusicLyrics.svelte?raw';
import player from '../components/music/MusicPlayer.svelte?raw';
import musicService from '../services/music.ts?raw';

// The Lyrics area's pure rules (ADR-051 D6; SPEC flows 11 and 13). The parser and the
// chain are the core's (dpc_client_core/music/lyrics.py) and tested there.

const LINES: LyricsLine[] = [
    { t_start: 1, text: 'one' },
    { t_start: 4, t_end: 6, text: 'two' },
    { t_start: 10, text: 'three' },
];

describe('the current line', () => {
    it('runs from its start to its end, else to the next line, else to the end of the track', () => {
        expect(currentLyricIndex(LINES, 0.5, 30)).toBe(-1);
        expect(currentLyricIndex(LINES, 1, 30)).toBe(0);
        expect(currentLyricIndex(LINES, 3.95, 30)).toBe(0);
        expect(currentLyricIndex(LINES, 4, 30)).toBe(1);
        expect(currentLyricIndex(LINES, 6.5, 30)).toBe(-1); // the gap after an explicit end
        expect(currentLyricIndex(LINES, 12, 30)).toBe(2);
        expect(currentLyricIndex(LINES, 30, 30)).toBe(-1);
        expect(currentLyricIndex(LINES, 500, 0)).toBe(2); // unknown duration: the last line stays
    });

    it('takes a line a hair early, as the prototype does', () => {
        expect(currentLyricIndex(LINES, 0.99, 30)).toBe(0);
    });

    it('is never an untimed line', () => {
        expect(currentLyricIndex([{ t_start: null, text: 'a' }], 5, 30)).toBe(-1);
    });
});

describe('the compact window: previous, current, next', () => {
    it('around a current line', () => {
        expect(lyricsWindow(LINES, 5, 30)).toEqual({ prev: 0, current: 1, next: 2 });
        expect(lyricsWindow(LINES, 1.5, 30)).toEqual({ prev: -1, current: 0, next: 1 });
        expect(lyricsWindow(LINES, 11, 30)).toEqual({ prev: 1, current: 2, next: -1 });
    });

    it('before the first line and in a gap, the next line is the one coming up', () => {
        expect(lyricsWindow(LINES, 0, 30)).toEqual({ prev: -1, current: -1, next: 0 });
        expect(lyricsWindow(LINES, 7, 30)).toEqual({ prev: 1, current: -1, next: 2 });
    });

    it('unsynced or empty lyrics have no current line', () => {
        expect(lyricsWindow([{ t_start: null, text: 'a' }], 3, 30)).toEqual({ prev: -1, current: -1, next: 0 });
        expect(lyricsWindow([], 3, 30)).toEqual({ prev: -1, current: -1, next: -1 });
    });
});

describe('stamping a line (Stamp current time)', () => {
    it('puts the stamp on the line at the cursor and moves to the next line', () => {
        const text = 'first\nsecond\nthird';
        const r = stampLrcLine(text, 0, 12.5);
        expect(r.text).toBe('[00:12.50] first\nsecond\nthird');
        expect(r.cursor).toBe('[00:12.50] first\n'.length);
        const r2 = stampLrcLine(r.text, r.cursor + 2, 15);
        expect(r2.text).toBe('[00:12.50] first\n[00:15.00] second\nthird');
        expect(r2.text.slice(r2.cursor)).toBe('third');
    });

    it('replaces the stamps a line had', () => {
        expect(stampLrcLine('[00:01.00][00:30.00] again', 5, 61.25).text).toBe('[01:01.25] again');
    });

    it('stamps the last line and leaves the cursor at the end', () => {
        const r = stampLrcLine('a\nb', 3, 2);
        expect(r.text).toBe('a\n[00:02.00] b');
        expect(r.cursor).toBe(r.text.length);
    });

    it('an empty line becomes an end mark; a metadata line is skipped', () => {
        expect(stampLrcLine('a\n\nb', 2, 3).text).toBe('a\n[00:03.00]\nb');
        const meta = stampLrcLine('[la:en]\nx', 1, 3);
        expect(meta.text).toBe('[la:en]\nx');
        expect(meta.cursor).toBe(8);
    });

    it('counts the lines Save would refuse', () => {
        expect(countUnstampedLines('[la:ru]\n[00:01.00] a\nb\n\n  c  \n[00:02.00]')).toBe(2);
        expect(countUnstampedLines('')).toBe(0);
    });
});

describe('stamps, LRC text and the badge', () => {
    it('writes mm:ss.xx', () => {
        expect(lrcStamp(0)).toBe('00:00.00');
        expect(lrcStamp(59.999)).toBe('01:00.00');
        expect(lrcStamp(125.5)).toBe('02:05.50');
        expect(lrcStamp(-1)).toBe('00:00.00');
        expect(lrcStamp(NaN)).toBe('00:00.00');
    });

    it('formats lyrics the way the core reads them back', () => {
        const ly: Lyrics = { track_hash: 'h', source: 'manual', language: 'en', lines: [
            { t_start: 1, text: 'a' }, { t_start: 2.5, t_end: 4, text: 'b' }] };
        expect(formatLrc(ly)).toBe('[la:en]\n[00:01.00] a\n[00:02.50] b <00:04.00>');
        expect(formatLrc({ track_hash: 'h', source: 'lrclib', synced: false, lines: [{ t_start: null, text: 'x' }] })).toBe('x');
        expect(formatLrc(null)).toBe('');
    });

    it('names the source, unsynced and edited', () => {
        expect(lyricsBadge(null)).toBe('none');
        expect(lyricsBadge({ track_hash: 'h', source: 'lrclib', synced: false, lines: [] })).toBe('lrclib · unsynced');
        expect(lyricsBadge({ track_hash: 'h', source: 'manual', edited: true, lines: [] })).toBe('manual · edited');
        expect(lyricsSynced({ track_hash: 'h', source: 'tags', lines: LINES })).toBe(true);
        expect(lyricsSynced({ track_hash: 'h', source: 'tags', synced: false, lines: [{ t_start: null, text: 'a' }] })).toBe(false);
    });
});

describe('LRCLIB in the panel', () => {
    it('says every outcome honestly', () => {
        const q = { artist: 'Band', title: 'Song', album: null, duration: 215 };
        expect(lrclibOutcomeText({ result: 'not_found', cached: true, query: q, record: null }))
            .toBe('LRCLIB: not found for "Band - Song", 215 s (from the local cache).');
        expect(lrclibOutcomeText({ result: 'instrumental', cached: false, query: q, record: null })).toContain('instrumental');
        expect(lrclibOutcomeText({ result: 'rate_limited', cached: false, query: q, record: null, retry_after: 30 })).toContain('30 s');
        expect(lrclibOutcomeText({ result: 'disabled', cached: false, query: null, record: null })).toContain('off');
        expect(lrclibOutcomeText({ result: 'error', cached: false, query: q, record: null, error: 'boom' })).toBe('LRCLIB: boom.');
        expect(lrclibOutcomeText({ result: 'mismatch', cached: false, query: q, record: { id: 1, artist_name: '', track_name: '', album_name: null, duration: 300 } }))
            .toContain('±2 s');
        expect(lrclibOutcomeText(null)).toBe('');
    });

    it('guesses artist and title from tags, else from "Artist - Title"', () => {
        const base: LibraryEntry = { id: 'trk_1', title: 'Band - Song', duration_s: 1, source: { kind: 'local', name: 'Band - Song.mp3', uri: '' } };
        expect(guessArtistTitle(base)).toEqual({ artist: 'Band', title: 'Song' });
        expect(guessArtistTitle({ ...base, artist: 'Tagged', title: 'Name' })).toEqual({ artist: 'Tagged', title: 'Name' });
        expect(guessArtistTitle({ ...base, title: 'lonely', source: { ...base.source, name: 'lonely.mp3' } })).toEqual({ artist: '', title: 'lonely' });
        expect(guessArtistTitle(null)).toEqual({ artist: '', title: '' });
    });

    it('the panel shows the egress note, and the webview never calls lrclib.net itself', () => {
        expect(LRCLIB_EGRESS_TEXT).toBe('Sends artist, title, album and duration to lrclib.net');
        expect(lyricsPanel).toContain('LRCLIB_EGRESS_TEXT');
        expect(lyricsPanel).toContain('Find on LRCLIB');
        expect(lyricsPanel).not.toMatch(/fetch\(|lrclib\.net\/api|XMLHttpRequest/);
        expect(lyricsPanel).toContain('Use LRCLIB lyrics (replaces current)');
    });

    it('opening a track never asks lrclib.net: the automatic load reads only get_music_lyrics', () => {
        const effect = lyricsPanel.slice(lyricsPanel.indexOf('// Another track: start over'), lyricsPanel.indexOf('let synced = $derived'));
        expect(effect).toContain('if (id) load(id);');
        expect(effect).not.toMatch(/findOnLrclib|lookupLyricsOnLrclib|lookup_music_lyrics_lrclib/);
        const load = lyricsPanel.slice(lyricsPanel.indexOf('async function load('), lyricsPanel.indexOf('// Another track: start over'));
        expect(load).toContain('getMusicLyrics(');
        expect(load).not.toMatch(/findOnLrclib|lookupLyricsOnLrclib|lookup_music_lyrics_lrclib/);
        // The lookup is reached only from a button.
        const calls = [...lyricsPanel.matchAll(/findOnLrclib\(/g)].length;
        const clicks = [...lyricsPanel.matchAll(/onclick=\{\(\) => findOnLrclib\(/g)].length;
        expect(clicks).toBe(2);
        expect(calls).toBe(clicks + 1); // + its definition
    });

    it('the lookup goes through the core command, never straight to lrclib.net', () => {
        expect(musicService).toContain("'lookup_music_lyrics_lrclib'");
        expect(musicService).not.toMatch(/fetch\(|lrclib\.net\/api|XMLHttpRequest/);
    });

    it('sits in the Listen view under Now playing', () => {
        const now = player.indexOf('aria-label="Now playing"');
        const lyr = player.indexOf('<MusicLyrics ');
        const upNext = player.indexOf('id="up-next-label"');
        expect(now).toBeGreaterThan(0);
        expect(lyr).toBeGreaterThan(now);
        expect(lyr).toBeLessThan(upNext);
    });
});

describe('replacing the lyrics the person wrote or edited', () => {
    const base: Lyrics = { track_hash: 'h', source: 'lrclib', lines: [{ t_start: 1, text: 'a' }] };

    it('manual or edited lyrics are their own; chain lyrics are not', () => {
        expect(lyricsIsOwnEdit({ ...base, source: 'manual' })).toBe(true);
        expect(lyricsIsOwnEdit({ ...base, source: 'manual', edited: true })).toBe(true);
        expect(lyricsIsOwnEdit({ ...base, source: 'tags', edited: true })).toBe(true);
        expect(lyricsIsOwnEdit(base)).toBe(false);
        expect(lyricsIsOwnEdit({ ...base, source: 'lrc' })).toBe(false);
        expect(lyricsIsOwnEdit(null)).toBe(false);
    });

    it('overwrite_manual is sent only with replace and only when confirmed', () => {
        expect(lrclibLookupPayload('trk_1', { artist: 'A', title: 'T' }))
            .toEqual({ track_id: 'trk_1', artist: 'A', title: 'T', replace: false });
        expect(lrclibLookupPayload('trk_1', {}, { replace: true }))
            .toEqual({ track_id: 'trk_1', replace: true });
        expect(lrclibLookupPayload('trk_1', {}, { replace: true, overwriteManual: true }))
            .toEqual({ track_id: 'trk_1', replace: true, overwrite_manual: true });
        expect(lrclibLookupPayload('trk_1', {}, { overwriteManual: true }))
            .toEqual({ track_id: 'trk_1', replace: false });
    });

    it('the panel asks with the confirm dialog of the app before it sends overwrite_manual', () => {
        expect(LYRICS_OVERWRITE_CONFIRM_TEXT).toContain('Replace your own lyrics');
        expect(lyricsPanel).toContain("import { confirmAsync } from '$lib/utils/dialog';");
        expect(lyricsPanel).not.toMatch(/window\.confirm|[^.\w]confirm\(/);
        const fn = lyricsPanel.slice(lyricsPanel.indexOf('async function findOnLrclib('), lyricsPanel.indexOf('async function startEdit('));
        const ask = fn.indexOf('confirmAsync(LYRICS_OVERWRITE_CONFIRM_TEXT');
        const guard = fn.indexOf('if (!yes || trackId !== id) return;');
        const send = fn.indexOf('lookupLyricsOnLrclib(');
        expect(fn).toContain('if (replace && lyricsIsOwnEdit(lyrics))');
        expect(ask).toBeGreaterThan(0);
        expect(guard).toBeGreaterThan(ask);
        expect(send).toBeGreaterThan(guard);
        expect(fn).toContain("a.code === 'overwrite_manual_required'");
    });
});

describe('Transcribe (the explicit step 4 of the chain)', () => {
    const transcribed: Lyrics = { track_hash: 'h', source: 'transcribe', lines: [{ t_start: 12.5, t_end: 15, text: 'First line' }] };
    const info = { provider: 'local_whisper', model: 'openai/whisper-large-v3-turbo', language: 'en', duration_s: 215, segments: 3, synced: true };
    const timeout: MusicAnswer<LyricsAnswer> = { ok: false, unavailable: false, code: 'failed', message: "Command 'transcribe_music_lyrics' timed out after 60 seconds" };
    const polled = (transcribe: any, lyrics: Lyrics | null = null): MusicAnswer<LyricsAnswer> => ({ ok: true, data: { lyrics, transcribe } });
    const noSleep = async () => {};

    it('the badge reads "transcribed", with unsynced when the lines have no times', () => {
        expect(lyricsBadge(transcribed)).toBe('transcribed');
        expect(lyricsBadge({ ...transcribed, synced: false, lines: [{ t_start: null, text: 'a' }] })).toBe('transcribed · unsynced');
    });

    it('overwrite_manual is sent only when the person confirmed', () => {
        expect(transcribeLyricsPayload('trk_1')).toEqual({ track_id: 'trk_1' });
        expect(transcribeLyricsPayload('trk_1', { overwriteManual: false })).toEqual({ track_id: 'trk_1' });
        expect(transcribeLyricsPayload('trk_1', { overwriteManual: true })).toEqual({ track_id: 'trk_1', overwrite_manual: true });
    });

    it('names the provider and model and claims no alignment quality', () => {
        const timed = transcriptionText(info);
        expect(timed).toContain('local_whisper (openai/whisper-large-v3-turbo)');
        expect(timed).toContain('3 lines timed from its segments');
        expect(timed).toContain('may be wrong');
        const plain = transcriptionText({ ...info, provider: 'openai', model: null, segments: 0, synced: false });
        expect(plain).toBe('Transcribed by openai: text only, no times (unsynced). Machine transcription: words and times may be wrong.');
        expect(transcriptionText({ ...info, provider: null, model: null })).toContain('an unnamed provider');
        expect(transcriptionText(null)).toBe('');
        for (const text of [timed, plain, TRANSCRIBE_NOTE_TEXT]) expect(text).not.toMatch(/accurate|aligned|precise|perfect/i);
        expect(TRANSCRIBE_NOTE_TEXT).toContain('misheard');
    });

    it('a direct answer is taken as it is', async () => {
        const r = await runTranscribe(async () => ({ ok: true, data: { lyrics: transcribed, applied: true, candidate: null, transcription: info } }),
            async () => { throw new Error('no poll on a direct answer'); });
        expect(r).toEqual({ ok: true, lyrics: transcribed, candidate: null, applied: true, info, kept: undefined });
    });

    it('a refusal or an error is surfaced without polling', async () => {
        let polls = 0;
        const poll = async () => { polls++; return polled(null); };
        const refused = await runTranscribe(async () => ({ ok: false, unavailable: false, code: 'overwrite_manual_required', message: 'needs confirmation' }), poll);
        expect(refused).toMatchObject({ ok: false, code: 'overwrite_manual_required' });
        const failed = await runTranscribe(async () => ({ ok: false, unavailable: false, code: 'transcription_failed', message: 'local Whisper (w): CUDA out of memory' }), poll);
        expect(failed).toMatchObject({ ok: false, code: 'transcription_failed', message: 'local Whisper (w): CUDA out of memory' });
        expect(polls).toBe(0);
    });

    it('past the command timeout it follows the run until it is done', async () => {
        expect(isCommandTimeout(timeout)).toBe(true);
        expect(isCommandTimeout({ ok: false, unavailable: false, code: 'failed', message: 'boom' })).toBe(false);
        const answers = [polled({ state: 'running' }), polled({ state: 'running' }),
                         polled({ state: 'done', applied: true, transcription: info }, transcribed)];
        const r = await runTranscribe(async () => timeout, async () => answers.shift()!, { sleep: noSleep });
        expect(answers).toEqual([]);
        expect(r).toEqual({ ok: true, lyrics: transcribed, candidate: null, applied: true, info, kept: undefined });
    });

    it('past the command timeout an error of the run is surfaced', async () => {
        const answers = [polled({ state: 'running' }), polled({ state: 'error', code: 'transcription_failed', message: 'GPU VRAM' })];
        const r = await runTranscribe(async () => timeout, async () => answers.shift()!, { sleep: noSleep });
        expect(r).toEqual({ ok: false, unavailable: false, code: 'transcription_failed', message: 'GPU VRAM' });
    });

    it('stops following when another track is opened, a run is lost, or it waits too long', async () => {
        let open = true;
        const cancelled = await runTranscribe(async () => timeout, async () => { open = false; return polled({ state: 'running' }); },
            { sleep: noSleep, cancelled: () => !open });
        expect(cancelled).toBeNull();
        const lost = await runTranscribe(async () => timeout, async () => polled(null), { sleep: noSleep });
        expect(lost).toMatchObject({ ok: false, code: 'lost' });
        let t = 0;
        const long = await runTranscribe(async () => timeout, async () => polled({ state: 'running' }),
            { sleep: async (ms) => { t += ms; }, now: () => t, intervalMs: 1000, maxWaitMs: 5000 });
        expect(long).toMatchObject({ ok: false, code: 'still_running' });
    });

    it('the panel asks with the confirm dialog before it sends overwrite_manual', () => {
        expect(TRANSCRIBE_OVERWRITE_CONFIRM_TEXT).toContain('Replace your own lyrics');
        const fn = lyricsPanel.slice(lyricsPanel.indexOf('async function transcribe('), lyricsPanel.indexOf('function stepText('));
        const guard = fn.indexOf('if (lyricsIsOwnEdit(lyrics))');
        const ask = fn.indexOf('confirmAsync(TRANSCRIBE_OVERWRITE_CONFIRM_TEXT');
        const stop = fn.indexOf('if (!yes || trackId !== id) return;');
        const yes = fn.indexOf('overwriteManual = true;');
        const send = fn.indexOf('transcribeMusicLyrics(id, { overwriteManual })');
        expect(guard).toBeGreaterThan(0);
        expect(ask).toBeGreaterThan(guard);
        expect(stop).toBeGreaterThan(ask);
        expect(yes).toBeGreaterThan(stop);
        expect(send).toBeGreaterThan(yes);
        expect(fn).toContain("r.code === 'overwrite_manual_required'");
        expect(fn).toContain('message = ');
    });

    it('a button next to Find on LRCLIB runs it, with a busy state; opening a track never does', () => {
        const start = lyricsPanel.indexOf('<div class="lookup">');
        const lookup = lyricsPanel.slice(start, lyricsPanel.indexOf('</div>', start));
        expect(lookup.indexOf('Find on LRCLIB')).toBeGreaterThan(0);
        expect(lookup.indexOf('onclick={transcribe}')).toBeGreaterThan(lookup.indexOf('Find on LRCLIB'));
        expect(lookup).toContain("busy === 'transcribe' ? 'Transcribing…' : 'Transcribe'");
        const effect = lyricsPanel.slice(lyricsPanel.indexOf('// Another track: start over'), lyricsPanel.indexOf('let synced = $derived'));
        const load = lyricsPanel.slice(lyricsPanel.indexOf('async function load('), lyricsPanel.indexOf('// Another track: start over'));
        for (const part of [effect, load]) expect(part).not.toMatch(/transcribe\(|transcribeMusicLyrics|transcribe_music_lyrics/);
        expect([...lyricsPanel.matchAll(/onclick=\{transcribe\}/g)].length).toBe(1);
        expect(musicService).toContain("'transcribe_music_lyrics'");
    });
});

import coreServiceRaw from '../coreService.ts?raw';
import { ALIGN_NOTE_TEXT, alignLyricsPayload } from './musicPlayer';

describe('Align to audio (forced alignment for unsynced lyrics)', () => {
    it('formats the badge as "aligned", with edited/unsynced suffixes', () => {
        expect(lyricsBadge({ track_hash: 'h', source: 'aligned', lines: LINES })).toBe('aligned');
        expect(lyricsBadge({ track_hash: 'h', source: 'aligned', edited: true, lines: LINES })).toBe('aligned · edited');
    });

    it('builds the align_music_lyrics payload cleanly', () => {
        expect(alignLyricsPayload('trk_1')).toEqual({ track_id: 'trk_1' });
        expect(alignLyricsPayload('trk_1', { overwriteManual: false })).toEqual({ track_id: 'trk_1' });
        expect(alignLyricsPayload('trk_1', { overwriteManual: true })).toEqual({ track_id: 'trk_1', overwrite_manual: true });
        expect(ALIGN_NOTE_TEXT).toContain('Whisper');
    });

    it('wires alignMusicLyrics in coreService.ts and services/music.ts and shows Align to audio on unsynced lyrics', () => {
        expect(coreServiceRaw).toContain("'align_music_lyrics'");
        expect(coreServiceRaw).toContain('export function alignMusicLyrics(');
        expect(musicService).toContain('alignMusicLyrics');
        expect(lyricsPanel).toContain('async function alignToAudio(');
        expect(lyricsPanel).toContain('onclick={alignToAudio}');
        expect(lyricsPanel).toContain("busy === 'align' ? 'Aligning…' : 'Align to audio'");
        const effect = lyricsPanel.slice(lyricsPanel.indexOf('// Another track: start over'), lyricsPanel.indexOf('let synced = $derived'));
        const load = lyricsPanel.slice(lyricsPanel.indexOf('async function load('), lyricsPanel.indexOf('// Another track: start over'));
        for (const part of [effect, load]) expect(part).not.toMatch(/alignToAudio|alignMusicLyrics|align_music_lyrics/);
    });
});
