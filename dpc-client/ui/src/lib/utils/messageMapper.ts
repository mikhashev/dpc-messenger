/**
 * Shared backend→frontend message mapping.
 * Single source of truth for all message field extraction and sender
 * identity resolution (ADR-031 §5: identity fields, not stored role).
 * Used by: AgentPanel (2 paths), MessageRouterPanel (group live),
 * HistorySyncPanel (group sync), +page.svelte (1:1 reloads).
 */

export interface MappedMessage {
    id: string;
    sender: string;
    senderName: string;
    text: string;
    timestamp: number;
    attachments: any[];
    msg_index: number;
    tool_calls: any[];
    thinking?: string;
    streamingRaw?: string;
    mentions?: any[];
    isAgent?: boolean;
    agentOwner?: string | null;
}

export interface SenderIdentityOptions {
    /** Conversation's own agent id (1:1 paths) — own-agent rows collapse to it */
    agentSelfId: string;
    agentSelfName?: string;
    selfNodeId?: string;
}

interface LocalCachedFields {
    thinking?: string;
    streamingRaw?: string;
    tool_calls?: any[];
}

interface MapOptions {
    fallbackSender?: string;
    fallbackSenderName?: string;
    index?: number;
    totalCount?: number;
    identity?: SenderIdentityOptions;
    local?: LocalCachedFields;
}

/**
 * Resolve sender/senderName/isAgent for 1:1 conversations from identity
 * fields. Stored role === 'assistant' stays as the legacy fallback for old
 * agent-side records that carry no sender_type/agent_owner.
 */
export function resolveSenderIdentity(
    msg: any,
    opts: SenderIdentityOptions,
): { sender: string; senderName: string; isAgent: boolean } {
    const ownAgent =
        msg.role === 'assistant'
        || msg.sender_name === opts.agentSelfId
        || (msg.sender_type === 'agent'
            && (msg.sender_node_id === opts.agentSelfId
                || msg.agent_owner === opts.agentSelfId
                || (!!opts.agentSelfName && msg.sender_name === opts.agentSelfName)));
    if (ownAgent) {
        return {
            sender: opts.agentSelfId,
            senderName: msg.sender_name || opts.agentSelfName || opts.agentSelfId,
            isAgent: true,
        };
    }
    if (!msg.sender_node_id || msg.sender_node_id === opts.selfNodeId) {
        return { sender: 'user', senderName: msg.sender_name || 'You', isAgent: false };
    }
    return {
        sender: msg.sender_node_id,
        senderName: msg.sender_name || msg.sender_node_id,
        isAgent: msg.sender_type === 'agent' || !!msg.is_agent,
    };
}

export function mapBackendMessage(msg: any, opts: MapOptions = {}): MappedMessage {
    const ts = msg.timestamp
        ? new Date(msg.timestamp).getTime()
        : Date.now() - ((opts.totalCount || 1) - (opts.index || 0)) * 1000;
    const id = msg.message_id || msg.id || `msg-${ts}-${opts.index || 0}`;
    const resolved = opts.identity ? resolveSenderIdentity(msg, opts.identity) : null;

    return {
        id,
        sender: resolved?.sender || opts.fallbackSender || msg.sender_node_id || msg.sender || 'unknown',
        senderName: resolved?.senderName || opts.fallbackSenderName || msg.sender_name || '',
        text: msg.content || msg.text || '',
        timestamp: ts,
        attachments: msg.attachments || [],
        msg_index: msg.msg_index || 0,
        tool_calls: msg.tool_calls || opts.local?.tool_calls || [],
        thinking: msg.thinking ?? opts.local?.thinking,
        streamingRaw: msg.streaming_raw ?? opts.local?.streamingRaw,
        mentions: msg.mentions || [],
        isAgent: resolved
            ? resolved.isAgent
            : (msg.sender_type === 'agent' || !!msg.agent_owner || msg.is_agent || msg.role === 'assistant' || false),
        agentOwner: msg.agent_owner || null,
    };
}
