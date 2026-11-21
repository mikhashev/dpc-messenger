import { describe, it, expect } from 'vitest';
import player from './music/MusicPlayer.svelte?raw';
import create from './music/MusicCreate.svelte?raw';
import review from './music/MusicReview.svelte?raw';
import reasonBox from './music/MusicReasonBox.svelte?raw';
import serviceRaw from '../services/music.ts?raw';
import jobs from '../services/musicJobs.ts?raw';
import coreRaw from '../coreService.ts?raw';

// No mount harness: the Create panel's wiring is checked in the source, like the
// window's (musicPlayerWindow.test.ts). Its rules are tested in utils/musicCreate.test.ts.
// A working copy may have CRLF line ends, so the sources are matched on LF.

const service = serviceRaw.replace(/\r\n/g, '\n');
const core = coreRaw.replace(/\r\n/g, '\n');

describe('the Create panel is opened from the controls row and Listen stays the default', () => {
    it('Create sits in the controls row, Listen in the header while in Create; each opening starts in Listen', () => {
        expect(player).toContain("let view = $state<'listen' | 'create'>('listen');");
        expect(player).toContain("view = 'listen';");
        // Mike 2026-11-22: not in the window header beside minimise/close.
        expect(player).toContain(`onclick={() => (view = 'create')} title="Make a song from your own text">Create</button>`);
        expect(player).toContain('title="Back to listening"\n            >Listen</button>'.replace(/\n/g, player.includes('\r\n') ? '\r\n' : '\n'));
        expect(player).not.toContain("{view === 'create' ? 'Listen' : 'Create'}");
        expect(player).toContain('<MusicCreate />');
    });

    it('the window leaves the panel its own keys', () => {
        expect(player).toContain("if (a.closest('[data-music-create]')) return;");
        expect(create).toContain('data-music-create');
    });
});

describe('events reach the panel through coreService', () => {
    it('both music events go to noteMusicEvent', () => {
        expect(core).toContain('message.event === "music_job_updated" || message.event === "music_engine_status"');
        expect(core).toContain('noteMusicEvent(message.event, message.payload);');
        expect(jobs).toContain('export function noteMusicEvent(');
    });

    it('a quiet active job is polled with get_music_job while the panel is open', () => {
        expect(jobs).toContain('jobsToPoll(get(musicJobs).jobs, lastSeen, Date.now())');
        expect(jobs).toContain('await getMusicJob(id)');
        expect(create).toContain('stopWatch = startJobWatch();');
    });

    it('"Stop X" is the existing stop_local_engine with the holder alias, on a press only', () => {
        expect(core).toContain("sendCommand('stop_local_engine', { alias })");
        expect(create).toContain('onclick={() => stopHolder(h.alias)}');
        expect(jobs.match(/stopLocalEngine\(/g)?.length).toBe(1);
    });
});

describe('review playback is the one playback path', () => {
    it('no component makes an audio element', () => {
        for (const src of [create, review, reasonBox]) {
            expect(src).not.toMatch(/<audio\b/);
            expect(src).not.toContain('new Audio(');
        }
    });

    it('the review goes through the service, which uses trackPlaybackUrl and waits for the duration', () => {
        expect(review).toContain('await reviewPlay(t.library_track_id, { at, autoplay, gain: lm.gain[i] ?? 1 });');
        const play = service.slice(service.indexOf('export async function reviewPlay('));
        expect(play).toContain('await trackPlaybackUrl(entry, await getPlaybackEnv())');
        expect(play).toContain('await waitForDuration(a, DURATION_WAIT_MS, () => token !== loadToken);');
    });

    it('the review never moves the queue on, and gives the element back when it closes', () => {
        expect(service).toContain("if (owner === 'review') musicReview.update(r => ({ ...r, playing: false }));\n        else void handleEnded(audioToken);");
        expect(review).toContain('void endReview();');
    });

    it('the level-match gain multiplies the person\'s volume', () => {
        expect(service).toContain("audio.volume = owner === 'review' ? v * reviewGain : v;");
    });
});

describe('decisions', () => {
    it('Discard and None of these go through the reason box, which cannot submit empty', () => {
        expect(reasonBox).toContain('const check = reasonCheck(text, reasonClass || null);');
        expect(review).toContain('onsubmit={(text, cls) => submitDiscard(t, text, cls)}');
        expect(review).toContain('onsubmit={submitNone}');
    });

    it('Escape closes the inline boxes', () => {
        expect(reasonBox).toContain("if (e.key === 'Escape') {");
        expect(review).toContain('function keepKeydown(e: KeyboardEvent) {');
    });

    it('a discard refreshes the library so the hidden take leaves every list', () => {
        expect(jobs).toContain('await noteTracksHidden([take.library_track_id]);');
        expect(service).toContain('export async function noteTracksHidden(');
    });
});

describe('the form picks the engine, YuE2 by default, and Start / Stop act on it', () => {
    it('engine, backbone and voice selects; the voice note; the short-cap warning', () => {
        expect(create).toContain('id="mc-engine"');
        expect(create).toContain('id="mc-backbone"');
        expect(create).toContain('id="mc-voice"');
        expect(create).toContain('<option value="">None</option>');
        expect(create).toContain('VOICE_NOTE');
        expect(create).toContain('shortCapWarning($musicCreateDraft, caps)');
    });

    it('the engine line and its buttons follow the selected engine', () => {
        expect(create).toContain('startEngine(selectedEngine,');
        expect(create).toContain('stopEngine(selectedEngine)');
        expect(create).toContain('fromTextSupport(caps, selectedEngine)');
        expect(core).toContain("callMusic(sendCommand, 'start_music_engine', payload)");
        expect(core).toContain("callMusic(sendCommand, 'stop_music_engine', engine ? { engine } : {})");
    });
});
