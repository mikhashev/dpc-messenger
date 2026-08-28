/**
 * Who gets to see a model's reasoning, decided by the surface rather than the sender.
 *
 * The rule that hid the thinking block was written against `isAiSender`, and it
 * was written for agent chats: an agent already shows its reasoning in the
 * rounds-and-tool-calls collapsible, so a second block under every answer was
 * noise. But reasoning only ever exists on AI messages, so a chat that marks its
 * own answers `sender: 'ai'` — which is exactly what the local AI chat does —
 * fell under the same label and could never draw the block at all.
 *
 * Observed 2026-08-13 on `ornith:9b-q8_0`: the log recorded 163 and 184
 * characters of thinking for two answers that showed none. Everything upstream
 * worked — the backend returned `thinking` and `thinking_tokens`, the router
 * copied both onto the message — and the last condition threw them away.
 *
 * So the question is not «is this an AI message» but «does this surface show the
 * reasoning anywhere else». One surface today answers no, and it is the one whose
 * user has just picked a thinking model from a dropdown.
 */

/**
 * Surfaces with no other place for an agent's reasoning.
 *
 * A set rather than a comparison so adding the next one is a line rather than a
 * boolean expression, and so the name carries the reason. Everything not in it
 * keeps the behaviour it has today — this changes one chat, not the rule.
 */
export const SURFACES_WITHOUT_THEIR_OWN_REASONING_VIEW = new Set(['local_ai']);

/** Whether the thinking block should be drawn under this message. */
export function showsThinkingBlock(
    conversationId: string,
    isAiSender: boolean,
): boolean {
    if (SURFACES_WITHOUT_THEIR_OWN_REASONING_VIEW.has(conversationId)) {
        return true;
    }
    // The original rule, unchanged: an agent chat and a group chat render rounds
    // and tool calls of their own, and do not need this block as well.
    return !isAiSender;
}
