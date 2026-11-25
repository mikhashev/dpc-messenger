import { describe, it, expect } from 'vitest';
import create from './music/MusicCreate.svelte?raw';
import prep from './music/MusicLyricsPrep.svelte?raw';
import core from '../coreService.ts?raw';

// No mount harness: the wiring of "Prepare lyrics" is checked in the source. Its rules are
// tested in utils/musicLyricsPrep.test.ts; the core side in the Python tests.

describe('Prepare lyrics in the Create panel', () => {
    it('sits under the lyrics box and opens the panel with the selected engine', () => {
        expect(create).toContain("import MusicLyricsPrep from './MusicLyricsPrep.svelte';");
        expect(create).toContain('>Prepare lyrics</button>');
        expect(create).toContain('<MusicLyricsPrep text={$musicCreateDraft.lyrics} engine={selectedEngine}');
        expect(create.indexOf('id="mc-lyrics"')).toBeLessThan(create.indexOf('>Prepare lyrics</button>'));
    });

    it('keeps the check in the draft and offers Undo only while the marked text is still in the box', () => {
        expect(create).toContain('lyricsPrep: state');
        expect(create).toContain('usedMarks.marked === $musicCreateDraft.lyrics');
        expect(create).toContain('onclick={undoMarks}');
    });

    it('reads the report through the read-only core command and never writes the text itself', () => {
        expect(core).toContain("'prepare_music_lyrics'");
        expect(prep).toContain('prepareMusicLyrics(sent,');
        expect(prep).toContain('onuse(report.marked_text, state)');
        // the lyrics are replaced only by the parent, on the press of "Use marked lyrics"
        expect(prep).not.toContain('musicCreateDraft');
        expect(prep).toContain('{useMarkedLabel(report)}');
    });

    it('does not re-check on every keystroke: the engine effect runs the check untracked', () => {
        expect(prep).toContain('untrack(() => void run())');
    });
});
