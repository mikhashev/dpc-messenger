/**
 * What the "Create New AI Chat / Agent" dialog validates and sends.
 *
 * An agent is two things: the `dpc_agent` runtime, which always lives on this
 * machine, and the LLM it reasons with, which may be served by a peer. The
 * dialog used to look the dialog's single provider alias (`dpc_agent` for an
 * agent) up in the chosen host's list. A peer never advertises `dpc_agent`
 * (the core keeps it out of what it serves), so with a peer as AI Host the
 * lookup failed with "Provider 'dpc_agent' not found." and no agent was made.
 *
 * Here the two lookups are separate: the runtime in the local list, the LLM in
 * the host's list. A plain chat has no runtime and keeps the old single lookup.
 */

export const AGENT_RUNTIME_ALIAS = 'dpc_agent';

export type ProviderRow = { alias: string; model?: string; context_window?: number };

export type NewChatTargetInput = {
    /** The dialog's "Chat Type" value; `dpc_agent` for an agent. */
    chatProvider: string;
    /** The agent's "AI Model (LLM)" value; ignored for a plain chat. */
    llmAlias: string;
    /** `local` or a peer's node_id. */
    host: string;
    /** The peer's display name, for messages; falls back to the node_id. */
    hostLabel?: string;
    /** This machine's providers. */
    localProviders: ProviderRow[];
    /** The host's providers: the same list for `local`, the peer's list otherwise. */
    hostProviders: ProviderRow[];
    /** Used for a local agent that was given no LLM. Never for a peer. */
    defaultProvider?: string;
};

export type NewChatTarget =
    | {
        ok: true;
        /** The row the chat is named after (a plain chat) or the runtime row (an agent). */
        provider: ProviderRow;
        /** The alias `create_agent` gets as `provider_alias`; `undefined` for a plain chat. */
        agentLlmAlias?: string;
        /** The LLM's context window, read from the host's list. */
        contextWindow?: number;
        /** What goes into `compute_host`: the peer's node_id, `undefined` for local. */
        computeHost?: string;
    }
    | { ok: false; message: string };

export function resolveNewChatTarget(input: NewChatTargetInput): NewChatTarget {
    const { chatProvider, llmAlias, host, localProviders, hostProviders } = input;
    const isLocal = host === 'local';
    const computeHost = isLocal ? undefined : host;
    const hostLabel = input.hostLabel || host;

    if (chatProvider !== AGENT_RUNTIME_ALIAS) {
        const provider = hostProviders.find((p) => p.alias === chatProvider);
        if (!provider) return { ok: false, message: `Provider '${chatProvider}' not found.` };
        return { ok: true, provider, computeHost };
    }

    // The runtime is this machine's, whoever serves the model.
    const runtime = localProviders.find((p) => p.alias === AGENT_RUNTIME_ALIAS);
    if (!runtime) return { ok: false, message: `Provider '${AGENT_RUNTIME_ALIAS}' not found.` };

    if (isLocal) {
        const alias = llmAlias || input.defaultProvider || AGENT_RUNTIME_ALIAS;
        const info = hostProviders.find((p) => p.alias === alias);
        return { ok: true, provider: runtime, agentLlmAlias: alias, contextWindow: info?.context_window, computeHost };
    }

    // A peer's model must be chosen: the local default is not the peer's to run.
    if (!llmAlias) {
        return { ok: false, message: `Pick a model offered by ${hostLabel}.` };
    }
    const info = hostProviders.find((p) => p.alias === llmAlias);
    if (!info) {
        return { ok: false, message: `${hostLabel} does not offer the model '${llmAlias}'.` };
    }
    return { ok: true, provider: runtime, agentLlmAlias: llmAlias, contextWindow: info.context_window, computeHost };
}

/** The alias the LLM select should show after the host changed: the host's first, never the runtime. */
export function defaultAgentLlmAlias(hostProviders: ProviderRow[] | null | undefined): string {
    return (hostProviders ?? []).find((p) => p.alias !== AGENT_RUNTIME_ALIAS)?.alias ?? '';
}
