import { describe, it, expect } from 'vitest';
import { filterNote, filterReportOf, type LyricsFilterReport } from './musicLyricsFilter';
import lyricsPanel from '../components/music/MusicLyrics.svelte?raw';

const base: LyricsFilterReport = { dropped: 0, kept_raw: false, split_by_words: 0, split_proportional: 0, words: true };

describe('filterNote', () => {
    it('says nothing when the filter changed nothing', () => {
        expect(filterNote(base)).toBeNull();
        expect(filterNote(null)).toBeNull();
        expect(filterNote(undefined)).toBeNull();
    });

    it('names how many lines were filtered, with the reasons and the dropped lines', () => {
        const note = filterNote({
            ...base,
            dropped: 3,
            dropped_reasons: { phrase: 2, loop: 1 },
            dropped_lines: [
                { start: 65, text: 'Subtitles by X', reason: 'phrase' },
                { start: null, text: 'Thanks for watching', reason: 'phrase' },
            ],
        });
        expect(note?.summary).toBe('Filtered 3 lines');
        expect(note?.facts[0]).toBe('2 credit or sign-off phrase, 1 looping text.');
        expect(note?.lines).toEqual([
            { time: '1:05', text: 'Subtitles by X', reason: 'credit or sign-off phrase' },
            { time: '–:––', text: 'Thanks for watching', reason: 'credit or sign-off phrase' },
        ]);
        expect(note?.more).toBe(1);
    });

    it('uses the singular for one line', () => {
        expect(filterNote({ ...base, dropped: 1, dropped_reasons: { repeat: 1 } })?.summary).toBe('Filtered 1 line');
    });

    it('reports the raw transcript kept when everything looked like noise', () => {
        const note = filterNote({ ...base, kept_raw: true });
        expect(note?.summary).toBe('Nothing filtered: raw transcript kept');
        expect(note?.facts.join(' ')).toContain('raw transcript was kept');
    });

    it('reports a fallback to segment times and approximate proportional splits', () => {
        const note = filterNote({ ...base, words: false, words_unavailable: true, split_proportional: 2 });
        expect(note?.facts.join(' ')).toContain('no word timestamps');
        expect(note?.facts.join(' ')).toContain('2 long segments were split in proportion to its text: those times are approximate');
    });

    it('always says where the unfiltered transcript is when it says anything', () => {
        expect(filterNote({ ...base, dropped: 1 })?.facts.at(-1)).toContain('kept beside the lyrics file');
    });
});

describe('filterReportOf', () => {
    const stored = { source: 'transcribe', transcription: { filter: { ...base, dropped: 2 } } };

    it('prefers the run answer, which lists the dropped lines', () => {
        const run = { filter: { ...base, dropped: 5, dropped_lines: [] } };
        expect(filterReportOf(run, stored)?.dropped).toBe(5);
    });

    it('falls back to the report stored on the transcribed lyrics', () => {
        expect(filterReportOf(null, stored)?.dropped).toBe(2);
    });

    it('ignores a stored report on lyrics from another source and a malformed one', () => {
        expect(filterReportOf(null, { ...stored, source: 'lrclib' })).toBeNull();
        expect(filterReportOf({ filter: { dropped: 'x' } }, null)).toBeNull();
        expect(filterReportOf(null, null)).toBeNull();
    });
});

describe('MusicLyrics.svelte shows the note', () => {
    it('renders a collapsed details element from filterNote with the raw view of the dropped lines', () => {
        expect(lyricsPanel).toContain("from '$lib/utils/musicLyricsFilter'");
        expect(lyricsPanel).toContain('filterNote(filterReportOf(transcribed, lyrics))');
        expect(lyricsPanel).toMatch(/<details class="filter-note[^"]*"/);
        expect(lyricsPanel).not.toMatch(/<details class="filter-note[^>]*\bopen\b/);
        expect(lyricsPanel).toContain('filterView.lines');
    });
});
