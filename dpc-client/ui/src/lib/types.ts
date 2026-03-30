// dpc-client/ui/src/lib/types.ts
// Shared TypeScript interfaces for coreService stores.
// All domain services import from here; coreService.ts re-exports for backward compat.

// --- Provider System ---

export interface ProviderInfo {
    alias: string;
    model: string;
    type: string;
    supports_vision: boolean;
    supports_voice?: boolean;  // v0.13.0+
}

export interface DefaultProvidersResponse {
    default_provider: string;
    vision_provider: string;
    voice_provider?: string;   // v0.13.0+
    agent_provider?: string;   // v0.18.0+
}

export interface ProvidersListResponse {
    providers: ProviderInfo[];
    default_provider: string;
    vision_provider: string;
}

// --- Agent System ---

export interface AgentInfo {
    agent_id: string;
    name: string;
    provider_alias: string;
    profile_name: string;
    instruction_set_name: string;
    created_at: string;
    updated_at?: string;
    compute_host?: string;
    // Telegram integration fields (v0.22.0+)
    telegram_enabled?: boolean;
    telegram_bot_token?: string;
    telegram_allowed_chat_ids?: string[];
    telegram_event_filter?: string[];
    telegram_max_events_per_minute?: number;
    telegram_cooldown_seconds?: number;
    telegram_transcription_enabled?: boolean;
    telegram_linked_at?: string;
    // Legacy field (deprecated in favor of telegram_allowed_chat_ids)
    telegram_chat_id?: string;
}

export interface AgentConfig {
    agent_id: string;
    name: string;
    provider_alias: string;
    profile_name: string;
    instruction_set_name: string;
    created_at: string;
    updated_at?: string;
    budget_usd?: number;
    max_rounds?: number;
}

// --- File Transfer ---

export interface FileTransfer {
    transfer_id: string;
    node_id: string;
    filename: string;
    direction: 'upload' | 'download';
    progress: number;          // 0-100
    status: 'pending' | 'transferring' | 'complete' | 'cancelled' | 'error';
    size_bytes?: number;
    mime_type?: string;
}

// --- Group Chat ---

export interface GroupChat {
    group_id: string;
    name: string;
    topic?: string;
    created_by: string;
    members: string[];         // node_ids
    version?: number;
}

// --- Node / Connection Status ---

export interface PeerInfo {
    node_id: string;
    display_name?: string;
    connection_strategy?: string;
    is_connected: boolean;
}

export interface NodeStatus {
    node_id: string;
    display_name?: string;
    mode: 'online' | 'offline' | 'degraded';
    peer_info?: PeerInfo[];
    connected_peers?: string[];
}

// --- P2P Messaging ---

export interface MessageAttachment {
    type: 'file' | 'image' | 'voice';
    filename?: string;
    thumbnail?: string;
    size_bytes?: number;
    voice_metadata?: Record<string, unknown>;
}

export interface P2PMessage {
    message_id: string;
    sender_node_id: string;
    sender_name?: string;
    text: string;
    timestamp?: number;
    attachments?: MessageAttachment[];
}

// --- Knowledge Commit ---

export interface VoteTally {
    approve: number;
    reject: number;
    total: number;
}

export interface KnowledgeCommit {
    proposal_id: string;
    topic: string;
    content: string;
    status?: 'pending' | 'approved' | 'rejected';
    vote_tally?: VoteTally;
    proposed_by?: string;
    entries?: unknown[];
}

// --- Voice Transcription ---

export interface VoiceTranscription {
    transfer_id: string;
    node_id: string;
    text: string;
    provider: string;
    confidence?: number;
    language?: string;
}
