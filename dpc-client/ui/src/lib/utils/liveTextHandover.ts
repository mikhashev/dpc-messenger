/**
 * The live streaming text is the only copy of the answer until the history has one.
 *
 * While an agent works, its answer exists in exactly one place: `agentStreamingText`,
 * accumulated from the chunk events. It becomes durable when the backend's history
 * payload arrives and replaces the conversation's message list. Between those two
 * moments the text is not a duplicate of anything — it is the answer.
 *
 * Two handlers used to wipe it without checking whether the handover had happened,
 * and either one alone reproduces AGENT-REPLY-VANISHES-UNTIL-YOU-LEAVE-THE-CHAT:
 *
 * 1. The history handler cleared it *before* calling `chatHistories.update`, and that
 *    update can decline — it refuses a backend payload shorter than what the UI holds
 *    (the B1 guard). Declined meant: text wiped, replacement not applied, nothing left.
 * 2. The task-completion handler (`agent_progress_clear`) cleared it outright at the
 *    end of the run — which is exactly the moment the user described, «когда ответ
 *    закончен, он исчезает» — without asking whether the reply had reached the history
 *    at all. If the history event is late or was declined, this wipes the only copy.
 *
 * Switching chats re-reads the history from disk, where the reply is by then, which is
 * why leaving and coming back made it reappear and made the bug look cosmetic.
 *
 * The invariant both rules below encode: **nothing drops the live text until a durable
 * copy exists.** Keeping it a moment too long shows the answer twice for one frame;
 * dropping it a moment too early loses the answer. Those costs are not symmetric.
 */

/**
 * Whether the backend payload may replace the UI's message list.
 *
 * Unchanged behaviour, named so it can be tested: a payload shorter than the UI's
 * own non-pending list is refused, because a truncated history must never overwrite
 * a complete one.
 */
export function historyUpdateApplies(
    incomingCount: number,
    existingNonPendingCount: number,
): boolean {
    return incomingCount >= existingNonPendingCount;
}

/**
 * Whether the history handler may drop the live text after running the update.
 *
 * Tied to `applied` and to nothing else. The equality *is* the fix: the two used to
 * be independent, the clear ran first, and a declined update left nothing behind.
 */
export function mayDropLiveTextAfterHistoryUpdate(applied: boolean): boolean {
    return applied;
}

/**
 * Whether the task-completion handler may drop the live text.
 *
 * It may, once the conversation's history actually ends in a reply — anything not
 * from the user. While the last message is still the user's turn, the agent's answer
 * exists only as live text and completion is the worst possible moment to discard it.
 * The pending-command condition is the pre-existing one and is kept: a DPC
 * `execute_ai_query` placeholder means the exchange is not finished.
 */
export function mayDropLiveTextOnCompletion(args: {
    lastMessageSender: string | null | undefined;
    hasPendingCommand: boolean;
}): boolean {
    if (args.hasPendingCommand) return false;
    if (!args.lastMessageSender) return false;
    return args.lastMessageSender !== 'user';
}
