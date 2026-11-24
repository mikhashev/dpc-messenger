// src/lib/utils/voiceTranscriptionAttach.ts
// Where a voice transcription event lands in the open chats (follow-up 11a).
//
// The backend binds every transcription to one conversation and sends its id as
// `conversation_id`; the chat of that id is the only one touched. Inside it the
// record is found by its message id when one of the chat's messages carries it
// (one id for every member's copy of a group voice message), else by transfer id
// or file name. File names are `voice_<UTC date_time>` to the second with no node
// part, so walking every chat by name gave one chat's words to another.

export interface VoiceTranscriptionEvent {
    conversation_id?: string;
    message_id?: string;
    transfer_id?: string;
    filename?: string;
}

function namesVoice(att: any, evt: VoiceTranscriptionEvent): boolean {
    return !!att && att.type === 'voice'
        && ((!!evt.transfer_id && att.transfer_id === evt.transfer_id)
            || (!!evt.filename && att.filename === evt.filename));
}

/**
 * The chat histories with `transcription` put on the voice attachment the event
 * names, in the event's conversation only. An event without a conversation id
 * changes nothing. Returns the same map when nothing matched.
 */
export function withVoiceTranscription(
    histories: Map<string, any[]>,
    evt: VoiceTranscriptionEvent,
    transcription: Record<string, unknown>,
): Map<string, any[]> {
    const chatId = evt.conversation_id;
    if (typeof chatId !== 'string' || !chatId) return histories;
    const messages = histories.get(chatId);
    if (!messages) return histories;

    const byId = !!evt.message_id && messages.some((m: any) => m?.id === evt.message_id);
    let changed = false;
    const updated = messages.map((message: any) => {
        if (!Array.isArray(message?.attachments)) return message;
        if (byId && message.id !== evt.message_id) return message;
        const voices = message.attachments.filter((att: any) => att?.type === 'voice');
        const named = message.attachments.filter((att: any) => namesVoice(att, evt));
        // The record named by its id takes the transcription on its voice attachment
        // even when the transfer id there is this node's own and the name differs.
        const targets = named.length > 0 ? named : (byId ? voices.slice(0, 1) : []);
        if (targets.length === 0) return message;
        changed = true;
        return {
            ...message,
            attachments: message.attachments.map((att: any) =>
                targets.includes(att) ? { ...att, transcription } : att),
        };
    });
    if (!changed) return histories;
    const next = new Map(histories);
    next.set(chatId, updated);
    return next;
}
