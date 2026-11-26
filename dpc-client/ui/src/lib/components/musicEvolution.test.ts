import { describe, expect, it } from 'vitest';
import evolutionRaw from './music/MusicEvolution.svelte?raw';

const evolution = evolutionRaw.replace(/\r\n/g, '\n');

describe('MusicEvolution wiring for full lyrics, seed, adapters, and who asked', () => {
    it('renders full lyrics in a collapsible expander and never JSON-stringifies params', () => {
        expect(evolution).not.toContain('JSON.stringify');
        expect(evolution).toContain('<details class="lyrics-expander">');
        expect(evolution).toContain('<pre class="lyrics-body">');
        expect(evolution).toContain('nodeLyricsText(');
        expect(evolution).toContain('lyricsSummary(');
    });

    it('resolves who asked using agentsList, groupChats, and nodeStatus and renders request block', () => {
        expect(evolution).toContain('agentsList');
        expect(evolution).toContain('groupChats');
        expect(evolution).toContain('nodeStatus');
        expect(evolution).toContain('formatRequestedBy(');
        expect(evolution).toContain('requestRows(');
        expect(evolution).toContain('paramRows(');
    });
});
