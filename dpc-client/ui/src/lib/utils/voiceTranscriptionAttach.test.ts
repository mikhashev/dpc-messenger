/**
 * A voice transcription event lands in the chat it belongs to, on the record it names.
 *
 * Follow-up 11a (Zcode #27, Johnny #25 P3-1): VoicePanel walked every open chat and
 * replaced the transcription of any voice attachment whose transfer id or file name
 * matched. File names are `voice_<UTC date_time>` to the second, so one chat's words
 * reached another chat, and a node off a group's roster reached the group's bubble.
 */

import { describe, it, expect } from "vitest";
import panel from "../panels/VoicePanel.svelte?raw";
import { withVoiceTranscription } from "./voiceTranscriptionAttach";

const VOICE = "voice_20261124_100000.webm";
const GROUP = "group-0f0f0f0f0f0f";
const PEER = "dpc-node-" + "d".repeat(32);
const said = { text: "what was said" };

function chats() {
    return new Map<string, any[]>([
        [GROUP, [
            { id: "m-from-b", attachments: [{ type: "voice", filename: VOICE, transfer_id: "t-b" }] },
            { id: "m-from-c", attachments: [{ type: "voice", filename: VOICE, transfer_id: "t-c" }] },
        ]],
        [PEER, [{ id: "p-1", attachments: [{ type: "voice", filename: VOICE, transfer_id: "t-peer" }] }]],
    ]);
}

const text = (h: Map<string, any[]>, chat: string, i: number) =>
    h.get(chat)![i].attachments[0].transcription?.text;

describe("withVoiceTranscription", () => {
    it("touches only the chat of the event's conversation, whatever else shares the file name", () => {
        const h = withVoiceTranscription(chats(), { conversation_id: PEER, filename: VOICE }, said);
        expect(text(h, PEER, 0)).toBe("what was said");
        expect(text(h, GROUP, 0)).toBeUndefined();
        expect(text(h, GROUP, 1)).toBeUndefined();
    });

    it("changes nothing for an event that names no conversation", () => {
        const before = chats();
        const h = withVoiceTranscription(before, { transfer_id: "t-peer", filename: VOICE }, said);
        expect(h).toBe(before);
        expect(text(h, PEER, 0)).toBeUndefined();
    });

    it("takes the record named by its message id when two records share the file name", () => {
        const h = withVoiceTranscription(chats(),
            { conversation_id: GROUP, message_id: "m-from-c", filename: VOICE, transfer_id: "t-other" }, said);
        expect(text(h, GROUP, 0)).toBeUndefined();
        expect(text(h, GROUP, 1)).toBe("what was said");
    });

    it("falls back to the transfer id or the name inside the chat when no message carries the id", () => {
        const h = withVoiceTranscription(chats(),
            { conversation_id: GROUP, message_id: "a-live-bubble-id", transfer_id: "t-b" }, said);
        expect(text(h, GROUP, 0)).toBe("what was said");
        expect(text(h, GROUP, 1)).toBeUndefined();
    });
});

describe("VoicePanel", () => {
    it("lands both transcription events through withVoiceTranscription, not a walk over every chat", () => {
        expect(panel.match(/withVoiceTranscription\(histories, evt,/g)?.length).toBe(2);
        expect(panel).not.toMatch(/for \(const \[chatId, messages\] of histories\)/);
    });
});
