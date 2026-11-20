import { describe, it, expect } from 'vitest';
import player from './music/MusicPlayer.svelte?raw';
import sidebar from './Sidebar.svelte?raw';
import page from '../../routes/+page.svelte?raw';
import service from '../services/music.ts?raw';

// No mount harness: the wiring of the Music Player (ADR-051 D1) is checked in the
// source, the way the other component guards do. Its rules are tested in
// utils/musicPlayer.test.ts.

describe('the Music Player button sits next to AGENTS MAP', () => {
    it('comes right after the AGENTS MAP button, styled like it, behind its own callback', () => {
        const map = sidebar.indexOf('<span class="agent-map-label">AGENTS MAP</span>');
        const music = sidebar.indexOf('<span class="agent-map-label">MUSIC PLAYER</span>');
        expect(map).toBeGreaterThan(0);
        expect(music).toBeGreaterThan(map);
        expect(sidebar).toContain('class="btn-agent-map btn-music-player" onclick={onOpenMusicPlayer}');
        expect(sidebar).toContain('onOpenMusicPlayer?: () => void;');
    });

    it('says what is playing, from the page-level store', () => {
        expect(sidebar).toContain("import { musicButtonSummary } from '$lib/services/music';");
        expect(sidebar).toContain('{$musicButtonSummary}');
    });
});

describe('the window is mounted once by the page', () => {
    it('is always in the page, opened and restored by the button', () => {
        expect(page).toContain('<MusicPlayer bind:open={showMusicPlayer} bind:minimised={musicPlayerMinimised} />');
        expect(page).toContain('onOpenMusicPlayer={() => { showMusicPlayer = true; musicPlayerMinimised = false; }}');
        // Not inside a chat branch: it stays mounted while chats switch.
        const mount = page.indexOf('<MusicPlayer ');
        expect(page.indexOf('<AgentMap bind:open={showAgentMap} />')).toBeLessThan(mount);
    });
});

describe('playback outlives the window', () => {
    it('the one audio element is made by the service, never by the window', () => {
        expect(service).toContain('new Audio()');
        expect(player).not.toMatch(/<audio\b/);
        expect(player).not.toContain('new Audio(');
    });

    it('closing the window changes nothing in playback', () => {
        const close = player.match(/function close\(\) \{([\s\S]*?)\n {2}\}/);
        expect(close).not.toBeNull();
        expect(close![1].trim()).toBe('open = false;');
    });
});

describe('the window follows the Agent map pattern', () => {
    it('remembers its box under its own key through windowGeometry', () => {
        expect(player).toContain('loadWindowState(storage(), viewport(), MUSIC_PLAYER_WINDOW)');
        expect(player).toContain('saveWindowState(storage(), { rect, minimised, miniWidth: miniWidth ?? undefined }, MUSIC_PLAYER_WINDOW)');
    });

    it('minimised, the bar has its own right-edge handle that resizes the width only', () => {
        const mini = player.slice(player.indexOf('<!-- Outside .mp-head'));
        expect(mini).toContain('class="mini-resize-handle"');
        expect(mini).toContain("onpointerdown={(e) => beginGesture(e, 'mini-resize')}");
        // The handle sits after .mp-head closes, so its press never reaches the bar's restore click.
        expect(player.indexOf('class="mini-resize-handle"')).toBeGreaterThan(player.indexOf('<div class="mp-buttons">'));
        expect(player).toContain("if (kind === 'mini-resize') e.stopPropagation();");
        expect(player).toContain("if (gesture.kind === 'mini-resize') miniWidth = resizeMiniWidth(s, dx, viewport());");
        expect(player).toMatch(/\.mini-resize-handle \{[^}]*cursor: ew-resize;[^}]*touch-action: none;/);
    });

    it('a narrow bar hides k / n, shuffle and repeat but keeps restore and close', () => {
        expect(player).toContain('{#if $musicPosition && miniShows.position}');
        expect(player).toContain('{#if miniShows.modes}{@render modeButtons(true)}{/if}');
        const buttons = player.slice(player.indexOf('<div class="mp-buttons">'), player.indexOf('{#if !minimised}'));
        expect(buttons).not.toContain('{#if');
    });

    it('minimised, the header bar is the mini-player: transport, title, k / n, shuffle and repeat', () => {
        const mini = player.slice(player.indexOf('<div class="mini">'), player.indexOf('{:else}', player.indexOf('<div class="mini">')));
        expect(mini).toContain('{@render transport(true)}');
        expect(mini).toContain('{$musicPosition}');
        expect(mini).toContain('{@render modeButtons(true)}');
        expect(mini).toContain('cur?.title');
    });

    it('takes keys only while focus is inside it, and never while typing', () => {
        expect(player).toContain('if (!a || !rootEl.contains(a)) return;');
        expect(player).toContain("if (a.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;");
    });

    it('does not use HTML5 drag and drop, which the webview\'s file drop takes on Windows', () => {
        expect(player).not.toMatch(/draggable=|ondragstart|ondrop/);
    });

    it('shows the unavailable sentence from the library state', () => {
        expect(player).toContain("$musicLibrary.status === 'unavailable'");
        expect(player).toContain('{$musicLibrary.message}');
    });
});
