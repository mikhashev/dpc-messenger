/**
 * The background models form's option lists and its one guard
 * (A-NODE-HAS-NO-ONE-SWITCH-THAT-KEEPS-ITS-BACKGROUND-MODELS-OFF-THE-GPU): a select must
 * always hold one of its options whatever a command answered, and the lists are the
 * backend readers' own sets — the embedding model knows mps, GLiNER does not.
 */

/** Where the embedding model may run ([memory] embedding_device). `auto` = an agent
 * profile's memory.embedding_device, else cuda, then mps, then cpu. */
export const EMBEDDING_DEVICES: readonly string[] = ['auto', 'cpu', 'cuda', 'mps'];

/** Where the GLiNER entity model may run ([knowledge_graph] gliner_device). `auto` =
 * an agent's own gliner_device in its config.json, else cuda when torch sees a card,
 * else cpu — no mps in the reader's set. */
export const GLINER_DEVICES: readonly string[] = ['auto', 'cpu', 'cuda'];

/** The agent graph stores ([knowledge_graph] backend). An agent's own kg_backend
 * override in its config.json still wins over this. */
export const KG_BACKENDS: readonly string[] = ['sqlite', 'grafeo'];

/** `text` when it names one of `allowed` (lowercased), else `fallback` — a select's
 * value must always be one of its options, whatever a command answered. */
export function allowedValue(text: unknown, allowed: readonly string[], fallback: string): string {
    const value = typeof text === 'string' ? text.trim().toLowerCase() : '';
    return allowed.includes(value) ? value : fallback;
}
