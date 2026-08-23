/**
 * Which agent the progress strip's speed is describing.
 *
 * The strip keeps the last non-empty speed it saw, on purpose: the burst of
 * events after a round carries tool arguments and per-tool narration with no
 * speed at all, and nulling on every event made the counter flash for
 * milliseconds and die. That rule is right inside one agent's run and wrong
 * across two, because "last non-empty" has no idea whose value it is holding.
 *
 * Two agents answering in the same chat is not hypothetical — it is the normal
 * shape of a group room. Only the llama.cpp provider ever sends a speed
 * (`llamacpp_server_provider.py`, the single writer of `usage["speed"]` in the
 * product), so an agent on a paid API sends none, inherits whatever the local
 * engine last left behind, and is displayed running at another model's tokens
 * per second, under another model's name, against another model's window.
 *
 * The fix is not to drop update-only. It is to remember whose run the value
 * belongs to, and to start empty when a different agent begins: an agent that
 * reports no speed then shows no speed, which is the truth about it.
 *
 * **The empty string is an agent, not an absence.** `agent_manager._emit_progress`
 * puts `"agent_id": self.agent_id or ''` on *every* `agent_progress` event, and
 * the singleton manager's `agent_id` is `None` — so `''` identifies the singleton
 * agent as surely as `ark` identifies Ark. Reading `''` as "this event says
 * nothing about the owner" would have left the singleton inheriting every named
 * agent's numbers, which is the defect this module exists to remove, surviving
 * in the one configuration most installs run. What means "says nothing" is the
 * field being **absent**, and `null` — not `''` — is what this module uses for a
 * strip that describes nobody yet.
 */

/** The fields of an agent-progress event this rule reads. */
export interface ProgressEventLike {
    agent_id?: string | null;
    speed?: Record<string, any> | null;
}

export interface StripUpdate {
    /** The agent the strip now describes; `null` when it describes nobody. */
    ownerAgentId: string | null;
    /** The speed to show, or null when a new agent's run has begun with none yet. */
    speed: Record<string, unknown> | null;
    /** The per-turn sample array must be emptied before appending. */
    resetSamples: boolean;
    /** The sample this event contributes, or null when it carries no speed. */
    appendSample: Record<string, any> | null;
}

/**
 * Fold one progress event into the strip's speed state.
 *
 * @param ownerAgentId the agent the current speed and samples belong to (`null` when none)
 * @param speed        the speed currently displayed
 * @param event        the incoming progress event
 */
export function nextStrip(
    ownerAgentId: string | null,
    speed: Record<string, unknown> | null,
    event: ProgressEventLike | null | undefined,
): StripUpdate {
    // Present-but-empty is an identity; absent is silence. See the note above.
    const speaksOfOwner =
        !!event && Object.prototype.hasOwnProperty.call(event, 'agent_id');
    const eventAgentId = speaksOfOwner ? (event!.agent_id ?? '') : ownerAgentId;
    const changedOwner = speaksOfOwner && eventAgentId !== ownerAgentId;

    // A new owner starts empty. This has to happen before the event's own speed
    // is applied, so the first round of the new agent replaces rather than joins.
    const carried = changedOwner ? null : speed;

    const sample = event?.speed ?? null;
    return {
        ownerAgentId: changedOwner ? eventAgentId : ownerAgentId,
        // Update-only within one run: no speed on this event leaves the last one
        // standing, because the tool-argument burst carries none.
        speed: sample ? (sample as Record<string, unknown>) : carried,
        resetSamples: changedOwner,
        appendSample: sample,
    };
}

/** The state a strip that describes nobody is in. Used on a chat switch. */
export function clearedStrip(): StripUpdate {
    return { ownerAgentId: null, speed: null, resetSamples: true, appendSample: null };
}
