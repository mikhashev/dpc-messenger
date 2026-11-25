// The Reasoning list and the window of a plain AI chat, read from the Text model
// the header has selected (THE-EMPTY-REASONING-OPTION-IS-LABELLED-AGENT-CONFIG-IN-
// A-CHAT-THAT-HAS-NO-AGENT, 2026-11-25 addendum; follow-up 13).
//
// The list used to be the group's copy (Agent config / Off / Low / Medium / High /
// Max) in a chat that has no agent. It now comes from the same source the Agent
// Configuration dialog reads: each providers row carries `reasoning_words` and
// `reasoning_default` (core declared_reasoning_words / effective_reasoning_default,
// e.g. the GGUF effort dictionary of a llama.cpp alias). The empty option sends
// nothing, so the model applies its own default, and says so.

export type ModelRow = {
  alias: string;
  context_window?: number | null;
  reasoning_words?: string[] | null;
  reasoning_default?: string | null;
};

/** The shared scale, for a model that names no words of its own (as in the agent dialog). */
export const FLEET_EFFORTS = ['low', 'medium', 'high', 'max'];

export type EffortOption = { value: string; label: string };

export type ChatEffortList = {
  /** The text of the empty option: what is sent is nothing. */
  emptyLabel: string;
  options: EffortOption[];
  /** True when the words are the model's own, false for the shared scale. */
  fromModel: boolean;
};

function capital(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

/**
 * The options for one model and the level this chat has stored. Off is always
 * offered: it is the switch every provider takes, not one of a model's words. A
 * stored level the model does not know stays on the list, marked, so switching
 * the model cannot silently clear it.
 */
export function chatEffortOptions(row: ModelRow | null | undefined, stored: string): ChatEffortList {
  const words = Array.isArray(row?.reasoning_words) && row!.reasoning_words!.length
    ? row!.reasoning_words!.filter((w) => w !== 'off')
    : null;
  const fallback = row?.reasoning_default || null;
  const base = words ?? FLEET_EFFORTS;
  const list = stored && stored !== 'off' && !base.includes(stored) ? [...base, stored] : [...base];
  const options: EffortOption[] = [{ value: 'off', label: 'Off' }];
  for (const w of list) {
    let label = capital(w);
    if (words && w === fallback) label += ' — default for this model';
    if (words && !words.includes(w)) label += ' — not in this model';
    options.push({ value: w, label });
  }
  const emptyLabel = words && fallback ? `Model default (${fallback})` : 'Model default';
  return { emptyLabel, options, fromModel: words !== null };
}

/**
 * The providers row behind a Text selection (`local:<alias>`, `remote:<node>:<alias>`),
 * or null when no list carries it.
 */
export function selectedModelRow<T extends ModelRow>(
  uniqueId: string,
  localRows: T[] | null | undefined,
  peerRows: Map<string, T[]> | null | undefined,
): T | null {
  if (!uniqueId) return null;
  if (uniqueId.startsWith('remote:')) {
    const [, node, ...rest] = uniqueId.split(':');
    const alias = rest.join(':');
    return (peerRows?.get(node) || []).find((r) => r.alias === alias) ?? null;
  }
  const alias = uniqueId.startsWith('local:') ? uniqueId.slice('local:'.length) : uniqueId;
  return (localRows || []).find((r) => r.alias === alias) ?? null;
}

/**
 * The window a plain AI chat's counter shows: the selected model's own
 * (`context_window`, the alias's configured one or its engine's), which is what the
 * backend sets on the next query; else what the backend last reported; else 0 (the
 * caller's default). Before this the counter fell to a fixed 16,384 whenever the
 * backend had not reported a number yet.
 */
export function aiChatWindow(row: ModelRow | null | undefined, reported: number): number {
  const own = Number(row?.context_window) || 0;
  if (own > 0) return own;
  return reported > 0 ? reported : 0;
}
