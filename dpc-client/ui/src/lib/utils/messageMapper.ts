/**
 * Shared backend→frontend message mapping.
 * Single source of truth for all message field extraction.
 * Used by: AgentPanel (2 paths), MessageRouterPanel (group live), HistorySyncPanel (group sync).
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

interface MapOptions {
    fallbackSender?: string;
    fallbackSenderName?: string;
    index?: number;
    totalCount?: number;
}

export function mapBackendMessage(msg: any, opts: MapOptions = {}): MappedMessage {
    const ts = msg.timestamp
        ? new Date(msg.timestamp).getTime()
        : Date.now() - ((opts.totalCount || 1) - (opts.index || 0)) * 1000;
    const id = msg.message_id || msg.id || `msg-${ts}-${opts.index || 0}`;

    return {
        id,
        sender: opts.fallbackSender || msg.sender_node_id || msg.sender || 'unknown',
        senderName: opts.fallbackSenderName || msg.sender_name || '',
        text: msg.content || msg.text || '',
        timestamp: ts,
        attachments: msg.attachments || [],
        msg_index: msg.msg_index || 0,
        tool_calls: msg.tool_calls || [],
        thinking: msg.thinking,
        streamingRaw: msg.streaming_raw,
        mentions: msg.mentions || [],
        isAgent: msg.sender_type === 'agent' || msg.is_agent || false,
        agentOwner: msg.agent_owner || null,
    };
}
