import { describe, it, expect } from 'vitest';
import {
  audioLengthLabel, attachmentCaption, musicTrackIdOf, showsOpenInPlayer, audioMetaParts,
  renderableAttachments, recordHasContent, openTrackPlan,
} from './musicAttachment';
import { mapBackendMessage } from './messageMapper';
import chatList from '../components/ChatMessageList.svelte?raw';
import page from '../../routes/+page.svelte?raw';
import service from '../services/music.ts?raw';

// The record chat_post_file writes (ADR-051 D40, D41): a `file` attachment with
// keep_agent_chat_attachment's sha256 and duration_seconds, a caption, and the
// optional `music` block that names the library track.
const take = {
  type: 'file' as const, filename: 'take_1.flac', size_bytes: 4_000_000, size_mb: 3.81,
  mime_type: 'audio/flac', status: 'completed', file_path: 'C:/Users/x/.dpc/conversations/agent_001/files/take_1.flac',
  sha256: 'ab'.repeat(32), duration_seconds: 192.4, caption: ' The first take ',
  music: { track_id: 'trk-1', take_id: 'take-1', job_id: 'job-1' },
};

describe('the length label', () => {
  it('is m:ss from duration_seconds', () => {
    expect(audioLengthLabel(take)).toBe('3:12');
  });
  it('is h:mm:ss past an hour', () => {
    expect(audioLengthLabel({ duration_seconds: 3725 })).toBe('1:02:05');
  });
  it('is empty when the record knows none: 0 is unmeasured', () => {
    expect(audioLengthLabel({ duration_seconds: 0 })).toBe('');
    expect(audioLengthLabel({})).toBe('');
    expect(audioLengthLabel(null)).toBe('');
    expect(audioLengthLabel({ duration_seconds: Number.NaN })).toBe('');
    expect(audioLengthLabel({ duration_seconds: -3 })).toBe('');
  });
  it('is on the meta line only when known', () => {
    expect(audioMetaParts(take)).toEqual(['3:12']);
    expect(audioMetaParts({ duration_seconds: 0 })).toEqual([]);
  });
});

describe('the caption', () => {
  it('is trimmed', () => {
    expect(attachmentCaption(take)).toBe('The first take');
  });
  it('is empty without one, or when it is not text', () => {
    expect(attachmentCaption({})).toBe('');
    expect(attachmentCaption({ caption: '   ' })).toBe('');
    expect(attachmentCaption({ caption: 5 as unknown as string })).toBe('');
  });
});

describe('Open in player', () => {
  it('shows for an audio file that names a library track', () => {
    expect(showsOpenInPlayer(take)).toBe(true);
    expect(musicTrackIdOf(take)).toBe('trk-1');
  });
  it('does not show without a track id, or with a blank one', () => {
    expect(showsOpenInPlayer({ ...take, music: undefined })).toBe(false);
    expect(showsOpenInPlayer({ ...take, music: { take_id: 'take-1' } })).toBe(false);
    expect(showsOpenInPlayer({ ...take, music: { track_id: '  ' } })).toBe(false);
    expect(musicTrackIdOf({ ...take, music: { track_id: '  ' } })).toBeNull();
  });
  it('does not show for a file that is not audio, or not a file', () => {
    expect(showsOpenInPlayer({ ...take, mime_type: 'application/pdf' })).toBe(false);
    expect(showsOpenInPlayer({ ...take, mime_type: undefined })).toBe(false);
    expect(showsOpenInPlayer({ ...take, type: 'image' })).toBe(false);
    expect(showsOpenInPlayer(null)).toBe(false);
  });
  it('shows even when this node holds no copy: the player streams the library track', () => {
    expect(showsOpenInPlayer({ ...take, file_path: undefined })).toBe(true);
  });
});

describe('what the button does', () => {
  const entry = { id: 'trk-1' };
  it('plays a library track', () => {
    expect(openTrackPlan(entry, null, false)).toBe('play');
    expect(openTrackPlan(entry, 'trk-2', true)).toBe('play');
  });
  it('leaves the track playing when it already plays', () => {
    expect(openTrackPlan(entry, 'trk-1', true)).toBe('already-playing');
  });
  it('plays a track that is loaded but paused', () => {
    expect(openTrackPlan(entry, 'trk-1', false)).toBe('play');
  });
  it('says so when the library does not hold the track', () => {
    expect(openTrackPlan(null, null, false)).toBe('missing');
  });
  it('does not play a hidden track', () => {
    expect(openTrackPlan({ id: 'trk-1', hidden: true }, null, false)).toBe('hidden');
  });
});

describe('an assistant record with a file and no text', () => {
  // `_record_answer` writes content "" for an answer that is only a file.
  const record = {
    id: 'm1', role: 'assistant', sender_node_id: 'agent_001', content: '',
    timestamp: '2026-11-24T10:00:00Z', attachments: [take],
  };

  it('keeps its attachment through the mapper, with the new fields', () => {
    const mapped = mapBackendMessage(record, { fallbackSender: 'agent_001' });
    expect(mapped.text).toBe('');
    expect(mapped.attachments).toHaveLength(1);
    expect(mapped.attachments[0].music).toEqual({ track_id: 'trk-1', take_id: 'take-1', job_id: 'job-1' });
    expect(mapped.attachments[0].caption).toBe(' The first take ');
    expect(mapped.attachments[0].sha256).toBe('ab'.repeat(32));
    expect(mapped.attachments[0].duration_seconds).toBe(192.4);
  });

  it('still yields a renderable attachment list, and has content to draw', () => {
    const mapped = mapBackendMessage(record, { fallbackSender: 'agent_001' });
    expect(renderableAttachments(mapped.attachments)).toHaveLength(1);
    expect(recordHasContent(mapped)).toBe(true);
  });

  it('a record with neither text nor a drawable attachment has nothing to draw', () => {
    expect(recordHasContent({ text: '', attachments: [] })).toBe(false);
    expect(recordHasContent({ text: '  ', attachments: null })).toBe(false);
    expect(recordHasContent({ text: '', attachments: [null, { filename: '' }] })).toBe(false);
  });

  it('one malformed entry does not take the others with it', () => {
    const list = [null, { filename: '' }, take, 'x' as unknown as { filename: string }];
    expect(renderableAttachments(list)).toEqual([take]);
  });
});

describe('the wiring', () => {
  it('the bubble draws the list through renderableAttachments, so a no-text record shows its file', () => {
    expect(chatList).toContain('{#each renderableAttachments(msg.attachments) as attachment}');
  });
  it('the bubble shows the length, the caption and an Open in player button', () => {
    expect(chatList).toContain('audioMetaParts(attachment)');
    expect(chatList).toContain('class="file-caption"');
    expect(chatList).toContain('>Open in player</button>');
    expect(chatList).toContain('onclick={() => openTrackInPlayer(trackId)}');
    expect(chatList).toContain('{#if showsOpenInPlayer(attachment)}');
  });
  it('the button asks the page to open the window through a store', () => {
    expect(service).toContain('export const musicWindowRequests = writable(0);');
    expect(service).toContain('musicWindowRequests.update(n => n + 1);');
    expect(page).toContain('if ($musicWindowRequests > 0) { showMusicPlayer = true; musicPlayerMinimised = false; }');
  });
  it('the player stays the one place that plays: the service queues the track and starts it', () => {
    const open = service.slice(service.indexOf('export async function openTrackInPlayer'));
    expect(open).toContain('enqueue(get(musicPlayback).queue, [trackId], true)');
    expect(open).toContain('return startAt(q, index, true);');
  });
});
