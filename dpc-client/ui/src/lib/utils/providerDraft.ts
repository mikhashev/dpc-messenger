/**
 * The "Add Provider" tab of ProvidersEditor keeps its draft in a form-local
 * `newProvider` object that only becomes a real provider through
 * `addNewProvider()`. Pressing "Save" while that draft holds typed content
 * used to save `editedConfig` as-is and drop the draft with no message
 * (Mike, 2026-09-28: a filled NeuralDeep form vanished on Save). This module
 * holds the pure predicate that decides whether a draft counts as "filled"
 * and therefore needs a decision from the user before Save proceeds, plus the
 * validity check "Add and save" must pass before it may call the real
 * `addNewProvider()` logic.
 */

import { looksLikeKey } from './apiKeyEnv';

/** The subset of the new-provider form this module reasons about. */
export interface ProviderDraft {
  alias?: string;
  type?: string;
  model?: string;
  api_key?: string;
  api_key_env?: string;
}

const isBlank = (v: string | undefined | null): boolean => (v ?? '').trim() === '';

/**
 * A draft is "filled" when the user has typed something that would be lost
 * silently — an alias, a model, or a key/key-name. Picking a provider type
 * from the dropdown does NOT count on its own: the type defaults (base_url,
 * context_window, etc.) are only applied by `addNewProvider()`, so a draft
 * that is still just "type changed to neuraldeep, everything else at its
 * initial default" carries nothing yet to lose.
 */
export function isProviderDraftFilled(draft: ProviderDraft): boolean {
  return (
    !isBlank(draft.alias) ||
    !isBlank(draft.model) ||
    !isBlank(draft.api_key) ||
    !isBlank(draft.api_key_env)
  );
}

export interface DraftValidation {
  valid: boolean;
  /** Present when `valid` is false — the reason to show the user on the form. */
  reason?: string;
}

/**
 * Whether the draft may become a provider via `addNewProvider()`. Mirrors the
 * existing "Add Provider" button's disabled condition (alias required, model
 * required unless dpc_agent, no raw key typed into the env-name field) and
 * adds the one check that button never had: the alias must not already name
 * a provider in this config, since `addNewProvider()` pushes unconditionally.
 */
export function validateProviderDraftForAdd(
  draft: ProviderDraft,
  existingAliases: readonly string[],
): DraftValidation {
  const alias = (draft.alias ?? '').trim();
  if (isBlank(alias)) {
    return { valid: false, reason: 'Give the new provider an alias before adding it.' };
  }
  if (draft.type !== 'dpc_agent' && isBlank(draft.model)) {
    return { valid: false, reason: 'Give the new provider a model before adding it.' };
  }
  if (looksLikeKey(draft.api_key_env)) {
    return {
      valid: false,
      reason: 'The API Key field holds a key, not an environment variable name — fix it before adding.',
    };
  }
  if (existingAliases.includes(alias)) {
    return { valid: false, reason: `A provider named "${alias}" already exists — pick a different alias.` };
  }
  return { valid: true };
}
