/**
 * A Telegram attachment reaches the screen twice: as the record the backend
 * stored (id `telegram-<kind>-<message_id>`, Telegram's own send time) and as
 * the websocket event that announced it. The backend now puts that id and time
 * on the event, so the live bubble replaces the stored copy instead of sitting
 * beside it. See A-TELEGRAM-VOICE-MESSAGE-SHOWS-TWICE-IN-THE-UI on the board.
 */

export interface Transcription {
    text: string;
    provider: string;
    [key: string]: any;
}

/**
 * Old Telegram history stored the transcription as a bare string; the player
 * reads `.text`, so a string drew an empty box. Accept both shapes.
 */
export function normalizeTranscription(t: unknown, provider = 'unknown'): Transcription | undefined {
    if (typeof t === 'string') return t ? { text: t, provider } : undefined;
    if (t && typeof t === 'object' && typeof (t as any).text === 'string') return t as Transcription;
    return undefined;
}

/** Same for every attachment of a mapped history message. */
export function normalizeAttachments(attachments: any[] | undefined | null): any[] {
    if (!Array.isArray(attachments)) return [];
    return attachments.map((a) => {
        if (a && a.type === 'voice' && typeof a.transcription === 'string') {
            return { ...a, transcription: normalizeTranscription(a.transcription, a.transcription_provider || 'unknown') };
        }
        return a;
    });
}

/** The bubble id for a Telegram event: the backend's, else a clock-derived one. */
export function telegramBubbleId(payload: { message_id?: string | null }, kind: string, now: number = Date.now()): string {
    const carried = payload?.message_id;
    return typeof carried === 'string' && carried.length > 0 ? carried : `telegram-${kind}-${now}`;
}

/** The event's time as epoch ms; the clock only when the event carries none. */
export function telegramBubbleTime(payload: { timestamp?: string | null }, now: number = Date.now()): number {
    const t = payload?.timestamp ? new Date(payload.timestamp).getTime() : NaN;
    return Number.isNaN(t) ? now : t;
}

/** Replace the entry with the same id in place, else append. */
export function upsertById<T extends { id?: string | null }>(messages: T[], entry: T): T[] {
    const i = entry.id ? messages.findIndex((m) => m.id === entry.id) : -1;
    if (i < 0) return [...messages, entry];
    const next = messages.slice();
    next[i] = entry;
    return next;
}
