import { describe, it, expect } from 'vitest';
import player from './music/MusicPlayer.svelte?raw';

// No mount harness: the settings card is checked in the source, the way the
// other component guards do. Its inputs sit in a flex-column .field, so the
// row toolbars' .text-input basis would be a height there, and the list's .num
// column is not an input class — both mistakes are guarded against here.

const cardStart = player.indexOf('<section class="card player-settings"');
const card = player.slice(cardStart, player.indexOf('</section>', cardStart));
const hints = card.match(/<span class="muted tiny">([\s\S]*?)<\/span>/g) ?? [];

describe('the player settings card inputs are styled as card fields', () => {
    it('the two number inputs are plain text-inputs, not the list\'s num column', () => {
        expect(card).toContain('<input id="mps-stream-port" class="text-input" type="number"');
        expect(card).toContain('<input id="mps-poll" class="text-input" type="number"');
        expect(card).not.toContain('class="text-input num"');
    });

    it('the card overrides the toolbars\' flex on its own inputs only', () => {
        expect(player).toMatch(/\.player-settings \.text-input \{[^}]*flex: 0 0 auto;/);
        expect(player).toMatch(/\.player-settings input\[type='number'\] \{[^}]*width:/);
        // Outside the card nothing changes: the list column and the toolbar rules stay.
        expect(player).toContain(
            '.num { width: 26px; flex: 0 0 26px; text-align: right; font-size: 12px; color: var(--text-secondary, #a9b0bb); font-variant-numeric: tabular-nums; }'
        );
        expect(player).toContain('.text-input { flex: 1 1 160px; min-width: 0; }');
    });
});

describe('the settings card hints tell a person what to decide', () => {
    it('no hint names an implementation detail', () => {
        expect(hints.length).toBeGreaterThan(0);
        for (const hint of hints) {
            expect(hint).not.toMatch(/tauri\.conf\.json|csp|webkitgtk|http range|loopback/i);
        }
    });

    it('says the four hints in these words', () => {
        expect(hints).toContain(
            '<span class="muted tiny">Where Export puts its MP3 and Opus copies. A full path, never a drive root; empty means the default folder. Applies at once.</span>'
        );
        expect(hints).toContain(
            '<span class="muted tiny">The port on this computer the player reads tracks from. Applies after the next backend restart. Keep 9996: another port works only after the app itself is rebuilt.</span>'
        );
        expect(hints).toContain(
            '<span class="muted tiny">How often the library looks for new files in its folders. Applies after the next backend restart.</span>'
        );
        expect(hints).toContain(
            '<span class="muted tiny">Applies at once. Leave it off on Linux: with it on, the Linux player there shows no track length and cannot jump forward. On is for testing only; Windows and macOS are not affected.</span>'
        );
        expect(card).toContain('<label class="field-label" for="mps-poll">Library check (seconds)</label>');
        expect(card).toContain('<span class="small">Track stream: byte ranges on Linux</span>');
    });
});
