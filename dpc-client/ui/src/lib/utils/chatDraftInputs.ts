/**
 * The input box holds one chat's draft; every other chat's draft waits in a map.
 *
 * ChatPanel has a single `currentInput` and a `chatDraftInputs` map keyed by chat
 * id. A switch parks the outgoing text in the map and loads the incoming chat's.
 * Anything that writes the draft after an `await` therefore has to say which chat
 * the text is for: writing `currentInput` then puts it into whichever chat is open
 * when the await returns. Dictation did exactly that — recorded in one chat, the
 * words landed in the chat the user had switched to while Whisper was working
 * (DICTATED-TEXT-LANDS-IN-WHICHEVER-CHAT-IS-OPEN-WHEN-THE-BUTTON-IS-PRESSED).
 */

export interface ChatDraftState {
    drafts: Map<string, string>;
    currentInput: string;
}

/**
 * Park the input box's text under the chat being left and load the one being opened.
 *
 * Unchanged behaviour, named so the switch-back can be tested together with a late
 * write: a chat with no parked draft opens empty.
 */
export function switchChatDraft(
    state: ChatDraftState,
    fromChatId: string,
    toChatId: string,
): ChatDraftState {
    const next = new Map(state.drafts).set(fromChatId, state.currentInput);
    const draft = next.get(toChatId);
    return { drafts: next, currentInput: draft !== undefined ? draft : '' };
}

/**
 * Append text to the draft of `targetChatId`, wherever that draft currently lives.
 *
 * `inputOwnerChatId` is the chat whose text the input box holds. When the target is
 * that chat the text goes into the box, as before; otherwise it goes into the parked
 * draft and the box is left alone. Nothing changes for empty text. The map is
 * returned as the same object when it was not touched.
 */
export function appendToChatDraft(
    state: ChatDraftState,
    inputOwnerChatId: string,
    targetChatId: string,
    text: string,
): ChatDraftState {
    if (!text) return state;
    if (targetChatId === inputOwnerChatId) {
        const input = state.currentInput;
        return { drafts: state.drafts, currentInput: input + (input ? ' ' : '') + text };
    }
    const parked = state.drafts.get(targetChatId) ?? '';
    return {
        drafts: new Map(state.drafts).set(targetChatId, parked + (parked ? ' ' : '') + text),
        currentInput: state.currentInput,
    };
}
