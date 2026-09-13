/**
 * Render `knowledge_extraction_fallback` into a sentence for a person.
 *
 * Unlike ExtractionFailureEvent, the backend sends no pre-built `message` for
 * this event — `reason` is the raw refusal text (`str(primary_error)`), not a
 * sentence. The panel used to have nowhere to put this event at all; without
 * a rendered sentence the peer's refusal stayed invisible even though the
 * extraction quietly ran on a fallback alias (THE-COLD-FALLBACK-HIDES-A-D2-REFUSAL).
 */

import type { KnowledgeExtractionFallbackEvent } from '$lib/types';

export function formatExtractionFallbackMessage(
    payload: KnowledgeExtractionFallbackEvent,
): string {
    const { node_id, requested_alias, reason, fallback_alias } = payload;
    const peer = node_id ? `${node_id.slice(0, 20)}...` : 'A peer';
    const alias = requested_alias ?? 'the requested provider';
    return `${peer} declined ${alias} (${reason}) — knowledge extraction ran on '${fallback_alias}' instead.`;
}
