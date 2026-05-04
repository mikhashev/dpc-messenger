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
  isAgent?: boolean;           // Agent message in group chat (v0.25.0+)
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
    name?: string;             // Short peer name (alias for display_name in some payloads)
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
    p2p_peers?: string[];      // Legacy: flat list of connected peer node_ids
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
    request_changes?: number;  // Revision-requested votes
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
    proposal?: { topic?: string; [key: string]: unknown }; // Nested proposal object (some payloads)
}

export interface KnowledgeEntry {
    content: string;
    tags: string[];
    confidence: number;
    cultural_specific: boolean;
    requires_context: string[];
    alternative_viewpoints: string[];
    edited_by?: string | null;
    edited_at?: string | null;
}

// Full proposal payload (knowledge_commit_proposed event) — superset of KnowledgeCommit.
// Used by KnowledgeCommitDialog.svelte for the commit review/vote UI.
export interface KnowledgeCommitProposal extends KnowledgeCommit {
    summary: string;
    entries: KnowledgeEntry[];   // Override base entries?: unknown[]
    participants: string[];
    cultural_perspectives: string[];
    alternatives: string[];
    devil_advocate: string | null;
    avg_confidence: number;
}

// --- Voice Transcription ---

export interface VoiceTranscription {
    transfer_id: string;
    node_id: string;
    text: string;
    provider: string;
    confidence?: number;
    language?: string;
    transcriber_node_id?: string;  // Node that performed transcription (v0.13.2+)
    timestamp?: string;            // ISO 8601 timestamp
    remote_provider_node_id?: string; // Remote provider node (if offloaded)
}

// --- Agent Event Payloads ---

export interface AgentProgressEvent {
    conversation_id: string;
    message?: string;
    round?: number;
    tool_name?: string;
    ts?: string;
}

export interface AgentProgressClearEvent {
    conversation_id: string;
}

export interface AgentTextChunkEvent {
    conversation_id: string;
    chunk: string;
    ts?: string;
}

export interface AgentTelegramLinkedEvent {
    agent_id: string;
    chat_id: string;
}

export interface AgentTelegramUnlinkedEvent {
    agent_id: string;
}

export interface AgentHistoryUpdatedEvent {
    conversation_id: string;
    messages: Array<{
        role: string;
        content: string;
        timestamp?: string;
        sender_name?: string;
        attachments?: MessageAttachment[];
    }>;
    tokens_used?: number;
    token_limit?: number;
    thinking?: string;
    message_count?: number;
    context_estimated?: number;
}

// --- File Transfer Event Payloads ---

export interface FileTransferOfferEvent {
    transfer_id: string;
    node_id: string;
    filename: string;
    size_bytes: number;
    mime_type?: string;
    hash?: string;
    sender_name?: string;
    group_id?: string;             // Present for group file transfers
    voice_metadata?: MessageAttachment['voice_metadata'];
}

export interface FileTransferProgressEvent {
    transfer_id: string;
    progress_percent: number;
    bytes_sent?: number;
    bytes_total?: number;
}

export interface FileTransferCompleteEvent {
    transfer_id: string;
    filename: string;
    file_path?: string;
    hash?: string;
    node_id?: string;
    direction?: 'upload' | 'download';
}

export interface FileTransferCancelledEvent {
    transfer_id: string;
    reason: string;
    filename?: string;
}

export interface FilePreparationStartedEvent {
    filename: string;
    size_mb: number;
    transfer_id?: string;
}

export interface FilePreparationProgressEvent {
    filename: string;
    phase: string;
    percent: number;
    transfer_id?: string;
}

export interface FilePreparationCompletedEvent {
    filename: string;
    hash: string;
    transfer_id?: string;
}

// --- Whisper Model Event Payloads ---

export interface WhisperModelEvent {
    model_name: string;
    provider?: string;         // Provider alias (used in loading events)
    provider_alias?: string;
    vram_freed_gb?: number;
    download_url?: string;
    size_mb?: number;
}

export interface WhisperModelFailedEvent extends WhisperModelEvent {
    error: string | null;      // Always present on failure events
}

// --- Group Chat Event Payloads ---

export interface GroupMessageEvent {
    group_id: string;
    message_id?: string;
    sender_node_id: string;
    sender_name?: string;
    text: string;
    timestamp?: number;
    attachments?: MessageAttachment[];
    mentions?: Mention[];
    is_agent?: boolean;
    sender_type?: string;
    agent_owner?: string | null;
}

export interface GroupFileEvent {
    group_id: string;
    message_id?: string;
    sender_node_id?: string;
    sender_name?: string;
    filename: string;
    transfer_id?: string;
    size_bytes?: number;
    mime_type?: string;
    text?: string;
    attachments?: MessageAttachment[];
}

export interface GroupMemberLeftEvent {
    group_id: string;
    node_id: string;
    member_name?: string;
    remaining_members: string[];
}

export interface GroupDeletedEvent {
    group_id: string;
    group_name?: string;
}

export interface GroupHistorySyncedEvent {
    group_id: string;
    messages: unknown[];
    message_count?: number;
}

// --- Telegram Event Payloads ---

export interface TelegramStatusEvent {
    enabled: boolean;
    connected: boolean;
    conversation_links?: Record<string, string>;
    bot_username?: string;
    error?: string;
}

export interface TelegramMessageEvent {
    conversation_id: string;
    telegram_chat_id: string;
    sender_name: string;
    text: string;
    timestamp: number;
}

export interface TelegramVoiceEvent {
    conversation_id: string;
    telegram_chat_id: string;
    sender_name: string;
    filename?: string;
    file_path?: string;
    transfer_id?: string;
    duration_seconds?: number;
    transcription?: MessageAttachment['transcription'];
}

export interface TelegramImageEvent {
    conversation_id: string;
    telegram_chat_id: string;
    sender_name: string;
    filename: string;
    caption?: string;
    file_path?: string;
    size_bytes?: number;
}

export interface TelegramFileEvent {
    conversation_id: string;
    telegram_chat_id: string;
    sender_name: string;
    filename: string;
    caption?: string;
    file_path?: string;
    mime_type?: string;
    size_bytes?: number;
}

// --- Session Event Payloads ---

export interface HistoryRestoredEvent {
    conversation_id: string;
    message_count: number;
    messages: unknown[];
}

export interface NewSessionProposalEvent {
    proposal_id: string;
    proposed_by: string;
    initiator_node_id: string;   // Alias for proposed_by (always present)
    conversation_id: string;
}

export interface NewSessionResultEvent {
    proposal_id: string;
    result: 'approved' | 'rejected' | 'timeout';
    conversation_id: string;
    sender_node_id: string;      // Legacy fallback for P2P chats (may be empty string)
}

export interface ConversationEvent {
    conversation_id: string;
}

export interface ConversationSettingsChangedEvent {
    conversation_id: string;
    persist_history: boolean;
}

// --- Knowledge Event Payloads ---

export interface ContextUpdatedEvent {
    node_id: string;
    context_hash: string;
}

export interface TokenWarningEvent {
    conversation_id: string;
    usage_percent: number;
    tokens_used: number;
    token_limit: number;
    estimated_tokens?: number;
    history_tokens?: number;     // Conversation history token count
    context_estimated?: number;  // Context window estimate from LLM API
}

export interface ExtractionFailureEvent {
    conversation_id: string;
    error: string;
    reason?: string;             // Alias for error in some payloads
}

export interface KnowledgeCommitResultEvent {
    proposal_id: string;
    status: 'approved' | 'rejected' | 'revision_needed';
    vote_tally: VoteTally;       // Always present in result events
    topic?: string;
}

// --- Provider Event Payloads ---

export interface AIResponseWithImageEvent {
    conversation_id: string;
    response: string;
    provider?: string;
    model?: string;
}
