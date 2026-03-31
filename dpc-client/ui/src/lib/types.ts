// dpc-client/ui/src/lib/types.ts
// Shared TypeScript interfaces for coreService stores.
// All domain services import from here; coreService.ts re-exports for backward compat.

// --- Chat Message Types ---
// Canonical definitions used across +page.svelte, ChatPanel, AgentPanel, ChatMessageList.
// Single source of truth — do not redefine locally in components.

export type Mention = {
  node_id: string;
  name: string;
  start: number;
  end: number;
};

export type MessageAttachment = {
  type: 'file' | 'image' | 'voice';
  filename: string;
  file_path?: string;          // Full-size image file path (P2P file transfers)
  size_bytes: number;
  size_mb?: number;
  hash?: string;
  mime_type?: string;
  transfer_id?: string;
  status?: string;
  data_url?: string;           // Base64 data URL (used in ChatPanel image preview)
  thumbnail?: string;          // Base64 thumbnail data URL
  // Image-specific fields (Phase 2.4):
  dimensions?: { width: number; height: number };
  vision_analyzed?: boolean;   // AI chat only: was vision API used?
  vision_result?: string;      // AI chat only: vision analysis text
  // Voice-specific fields (v0.13.0):
  voice_metadata?: {
    duration_seconds: number;
    sample_rate: number;
    channels: number;
    codec: string;
    recorded_at: string;
  };
  // Voice transcription (v0.13.2+):
  transcription?: {
    text: string;
    provider: string;
    transcriber_node_id?: string;
    confidence?: number;
    language?: string;
    timestamp?: string;
    remote_provider_node_id?: string;
  };
};

export type Message = {
  id: string;
  sender: string;
  senderName?: string;         // Display name for the sender (peer name or model name)
  text: string;
  timestamp: number;
  commandId?: string;
  model?: string;              // AI model name (for AI responses)
  streamingRaw?: string;       // v0.16.0+: Raw streaming text (shown in collapsible)
  thinking?: string;           // Thinking/reasoning content (v1.4+)
  thinkingTokens?: number;     // Tokens used for thinking (v1.4+)
  mentions?: Mention[];        // @-mentions in group chat messages
  attachments?: MessageAttachment[];
  isError?: boolean;           // Error message styling (v0.19.2+)
};

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
