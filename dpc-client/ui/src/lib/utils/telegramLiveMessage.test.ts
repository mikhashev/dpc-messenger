import { describe, it, expect } from 'vitest';
import { mapBackendMessage } from './messageMapper';
import { mergeBackfillWithLive } from './liveMessageIdentity';
import {
    normalizeTranscription,
    telegramBubbleId,
    telegramBubbleTime,
    upsertById,
} from './telegramLiveMessage';

const STORED_ID = 'telegram-voice-77';
const SENT_AT = '2026-09-30T09:36:09+00:00';

/** Shaped like the record the backend stores for a Telegram voice message. */
const stored = {
    message_id: STORED_ID,
    id: STORED_ID,
    role: 'user',
    content: 'hello there',
    timestamp: SENT_AT,
    sender_name: 'Mike',
    attachments: [{ type: 'voice', filename: 'v.ogg', file_path: '/x/v.ogg', transcription: 'hello there' }],
};

/** What TelegramPanel builds from the event. */
const liveBubble = (payload: any) => ({
    id: telegramBubbleId(payload, 'voice'),
    timestamp: telegramBubbleTime(payload),
    text: 'hello there',
    attachments: [{ type: 'voice', transcription: normalizeTranscription(payload.transcription, payload.transcription_provider) }],
});

describe('a Telegram voice event arriving after the stored copy', () => {
    const event = { message_id: STORED_ID, timestamp: SENT_AT, transcription: 'hello there', transcription_provider: 'whisper_local' };

    it('replaces the stored copy: one entry, the backend time', () => {
        const history = [mapBackendMessage(stored, { index: 0, totalCount: 1 })];
        const next = upsertById(history as any[], liveBubble(event) as any);

        expect(next).toHaveLength(1);
        expect(next[0].timestamp).toBe(new Date(SENT_AT).getTime());
        expect(next[0].attachments[0].transcription.text).toBe('hello there');
    });

    it('still appends when the event arrives first and the copy is not there yet', () => {
        expect(upsertById([], liveBubble(event) as any)).toHaveLength(1);
    });

    it('leaves one entry when the history is opened after the event', () => {
        const live = liveBubble(event);
        const loaded = [mapBackendMessage(stored, { index: 0, totalCount: 1 })];
        expect(mergeBackfillWithLive(loaded as any[], [live as any])).toHaveLength(1);
    });

    it('falls back to a clock id only when the event carries none', () => {
        expect(telegramBubbleId({}, 'voice', 5)).toBe('telegram-voice-5');
        expect(telegramBubbleTime({}, 9)).toBe(9);
    });
});

describe('a history entry with a string transcription', () => {
    it('reaches the player as {text, provider}', () => {
        const mapped = mapBackendMessage(stored, { index: 0, totalCount: 1 });
        expect(mapped.attachments[0].transcription).toEqual({ text: 'hello there', provider: 'unknown' });
    });

    it('leaves the object shape alone', () => {
        const obj = { text: 'x', provider: 'p', confidence: 0.9 };
        const mapped = mapBackendMessage({ ...stored, attachments: [{ type: 'voice', transcription: obj }] }, { index: 0, totalCount: 1 });
        expect(mapped.attachments[0].transcription).toBe(obj);
    });

    it('shows no box for an empty string', () => {
        expect(normalizeTranscription('')).toBeUndefined();
    });
});
