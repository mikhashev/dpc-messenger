/**
 * The payload the Agent Models Configuration dialog sends.
 *
 * Extracted from Sidebar.svelte because the difference between "the user chose
 * Default (global)" and "the caller did not mention this field" lives here, and
 * it was wrong: the dialog turned an empty selection into `null`, and the
 * backend reads `null` as "leave unchanged". So the three optional model
 * dropdowns could be set but never cleared — the dialog reopened showing the
 * old model and it looked like saving was broken.
 *
 * The two meanings are kept apart deliberately:
 *   ''    — the user picked `<option value="">Default (global)</option>`; clear it
 *   null  — this call is not about that field at all
 */
export type AgentModelConfigState = {
  providerAlias: string;
  sleepProvider: string;
  snapshotProvider: string;
  snapshotThreshold: number | string;
  compactionEnabled: boolean;
  compactionProvider: string;
  compactionThreshold: number | string;
  retrievalVector: 'native' | 'grafeo';
  retrievalText: 'native' | 'grafeo';
};

export type AgentModelConfigPayload = {
  provider_alias: string;
  sleep_provider_alias: string;
  snapshot_summarize_provider: string;
  snapshot_summarize_threshold: number | null;
  compaction_enabled: boolean;
  compaction_provider: string;
  compaction_threshold: number | null;
  retrieval_vector: 'native' | 'grafeo';
  retrieval_text: 'native' | 'grafeo';
};

export function buildAgentModelConfigPayload(
  state: AgentModelConfigState,
): AgentModelConfigPayload {
  return {
    provider_alias: state.providerAlias,
    // The three below carry the empty string through on purpose — see the note
    // above. The numeric fields keep `null` for "unset", where there is no
    // "clear to default" to express: both have a documented default value.
    sleep_provider_alias: state.sleepProvider,
    snapshot_summarize_provider: state.snapshotProvider,
    snapshot_summarize_threshold:
      Number(state.snapshotThreshold) > 0 ? Number(state.snapshotThreshold) : null,
    compaction_enabled: state.compactionEnabled,
    compaction_provider: state.compactionProvider,
    compaction_threshold:
      Number(state.compactionThreshold) > 0 ? Number(state.compactionThreshold) : null,
    retrieval_vector: state.retrievalVector,
    retrieval_text: state.retrievalText,
  };
}
