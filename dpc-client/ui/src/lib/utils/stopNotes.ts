/**
 * The status lines a Stop puts in the live progress panel: "Stop pressed by Mike",
 * "Writing a summary ... tools are off", "Kill pressed by Mike", "Stopped before the
 * first step". The backend sends them on the ordinary `agent_progress` event with
 * `stop_note` set (loop.py STOP_NOTE_KINDS, agent_manager.py `_interrupt_loop`).
 *
 * They are kept apart from the progress the panel draws as thinking and tool rows:
 * a Stop line must not replace the tool that is still running, and it must not
 * count as a round. One list per conversation, cleared when the run ends.
 */

export type StopNoteKind = 'pressed' | 'summary' | 'kill' | 'before_round_1';

export interface StopNote {
    kind: StopNoteKind;
    text: string;
    ts?: string;
}

export type StopNotes = Record<string, StopNote[]>;

const KINDS: ReadonlySet<string> = new Set(['pressed', 'summary', 'kill', 'before_round_1']);

/** A progress payload that is a Stop line (and not thinking or a tool). */
export function isStopNote(payload: { stop_note?: unknown } | null | undefined): boolean {
    return !!payload && typeof payload.stop_note === 'string' && KINDS.has(payload.stop_note);
}

/** The notes with this payload's line added at the end. A line of a kind already
 *  shown is not drawn twice (a repeated press says nothing new). */
export function addStopNote(
    notes: StopNotes,
    payload: { conversation_id?: string; stop_note?: string; message?: string; ts?: string },
): StopNotes {
    const cid = payload.conversation_id;
    if (!cid || !isStopNote(payload) || !payload.message) return notes;
    const held = notes[cid] ?? [];
    if (held.some((n) => n.kind === payload.stop_note)) return notes;
    const note: StopNote = { kind: payload.stop_note as StopNoteKind, text: payload.message, ts: payload.ts };
    return { ...notes, [cid]: [...held, note] };
}

/** The notes without this conversation's: its run is over. */
export function clearStopNotes(notes: StopNotes, conversationId: string | undefined): StopNotes {
    if (!conversationId || !(conversationId in notes)) return notes;
    const next = { ...notes };
    delete next[conversationId];
    return next;
}
