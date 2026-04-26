<!-- Sidebar.svelte - Extracted sidebar navigation and connection management -->
<!-- Displays connection status, node info, peer list, chat list, and action buttons -->

<script lang="ts">
  // State for Telegram linking dialog
  let showTelegramLinkDialog = $state(false);
  let linkingAgentId = $state('');
  let linkErrorMessage = $state('');

  // State for Agent Model Config popup
  let showModelConfigPopup = $state(false);
  let modelConfigAgentId = $state('');
  let modelConfigProviderAlias = $state('');
  let modelConfigSleepProvider = $state('');
  let modelConfigProvidersList = $state<{alias: string, model: string, type: string}[]>([]);
  let modelConfigSaving = $state(false);

  // All available agent event types (mirrors EVENT_EMOJIS in agent_telegram_bridge.py)
  const ALL_EVENT_TYPES: { key: string; label: string }[] = [
    { key: 'task_started',              label: 'Task Started' },
    { key: 'task_completed',            label: 'Task Completed' },
    { key: 'task_failed',               label: 'Task Failed' },
    { key: 'task_scheduled',            label: 'Task Scheduled' },
    { key: 'agent_message',             label: 'Agent Message' },
    { key: 'agent_started',             label: 'Agent Started' },
    { key: 'agent_stopped',             label: 'Agent Stopped' },
    { key: 'tool_executed',             label: 'Tool Executed' },
    { key: 'budget_warning',            label: 'Budget Warning' },
    { key: 'rate_limit_hit',            label: 'Rate Limit Hit' },
    { key: 'code_modified',             label: 'Code Modified' },
    { key: 'knowledge_updated',         label: 'Knowledge Updated' },
    { key: 'identity_updated',          label: 'Identity Updated' },
    { key: 'scratchpad_updated',        label: 'Scratchpad Updated' },
    { key: 'sleep_state_changed',       label: 'Sleep Events' },
  ];
  const DEFAULT_EVENT_FILTER = 'task_started,task_completed,task_failed,code_modified,budget_warning,rate_limit_hit,agent_message';

  function isEventSelected(key: string): boolean {
    return telegramEventFilter.split(',').map((e: string) => e.trim()).includes(key);
  }

  function toggleEventType(key: string): void {
    const current = new Set(telegramEventFilter.split(',').map((e: string) => e.trim()).filter((e: string) => e));
    if (current.has(key)) {
      current.delete(key);
    } else {
      current.add(key);
    }
    telegramEventFilter = [...current].join(',');
  }

  // Telegram configuration fields
  let telegramBotToken = $state('');
  let telegramAllowedChatIds = $state('');
  let telegramEventFilter = $state(DEFAULT_EVENT_FILTER);
  let telegramMaxEventsPerMinute = $state(20);
  let telegramCooldownSeconds = $state(3.0);
  let telegramTranscriptionEnabled = $state(true);
  let telegramUnifiedConversation = $state(false);

  // Handle Telegram link button click
  function handleLinkTelegram(agentId: string) {
    const agent = agents.find(a => a.agent_id === agentId);
    linkingAgentId = agentId;

    // Pre-populate with existing config or defaults
    telegramBotToken = agent?.telegram_bot_token || '';
    telegramAllowedChatIds = agent?.telegram_allowed_chat_ids?.join(', ') || '';
    telegramEventFilter = agent?.telegram_event_filter?.join(',') || DEFAULT_EVENT_FILTER;
    telegramMaxEventsPerMinute = agent?.telegram_max_events_per_minute || 20;
    telegramCooldownSeconds = agent?.telegram_cooldown_seconds || 3.0;
    telegramTranscriptionEnabled = agent?.telegram_transcription_enabled !== false;
    telegramUnifiedConversation = agent?.telegram_unified_conversation === true;

    linkErrorMessage = '';
    showTelegramLinkDialog = true;
  }

  // Confirm Telegram link
  async function confirmTelegramLink() {
    if (!telegramBotToken.trim() || !telegramAllowedChatIds.trim()) {
      linkErrorMessage = 'Bot token and at least one chat ID are required';
      return;
    }

    if (onLinkAgentTelegram) {
      try {
        const chatIds = telegramAllowedChatIds.split(',').map(id => id.trim()).filter(id => id);
        const eventFilter = telegramEventFilter.split(',').map(e => e.trim()).filter(e => e);

        await onLinkAgentTelegram(linkingAgentId, {
          bot_token: telegramBotToken.trim(),
          chat_ids: chatIds,
          event_filter: eventFilter,
          max_events_per_minute: telegramMaxEventsPerMinute,
          cooldown_seconds: telegramCooldownSeconds,
          transcription_enabled: telegramTranscriptionEnabled,
          unified_conversation: telegramUnifiedConversation,
        });

        showTelegramLinkDialog = false;
        telegramBotToken = '';
        telegramAllowedChatIds = '';
        telegramEventFilter = DEFAULT_EVENT_FILTER;
        telegramMaxEventsPerMinute = 20;
        telegramCooldownSeconds = 3.0;
        telegramTranscriptionEnabled = true;
        telegramUnifiedConversation = false;
        linkErrorMessage = '';
      } catch (error: any) {
        linkErrorMessage = error.message || 'Failed to link agent to Telegram';
      }
    }
  }

  // Unlink agent from Telegram (from dialog)
  async function handleUnlinkFromDialog() {
    if (onUnlinkAgentTelegram && linkingAgentId) {
      try {
        await onUnlinkAgentTelegram(linkingAgentId);
        showTelegramLinkDialog = false;
        telegramBotToken = '';
        telegramAllowedChatIds = '';
        telegramEventFilter = DEFAULT_EVENT_FILTER;
        telegramMaxEventsPerMinute = 20;
        telegramCooldownSeconds = 3.0;
        telegramTranscriptionEnabled = true;
        telegramUnifiedConversation = false;
        linkErrorMessage = '';
        linkingAgentId = '';
      } catch (error: any) {
        linkErrorMessage = error.message || 'Failed to unlink agent from Telegram';
      }
    }
  }

  // Cancel Telegram link
  function cancelTelegramLink() {
    showTelegramLinkDialog = false;
    telegramBotToken = '';
    telegramAllowedChatIds = '';
    telegramEventFilter = 'task_completed,task_failed,agent_message';
    telegramMaxEventsPerMinute = 20;
    telegramCooldownSeconds = 3.0;
    telegramTranscriptionEnabled = true;
    telegramUnifiedConversation = false;
    linkErrorMessage = '';
    linkingAgentId = '';
  }

  // Handle model config badge click
  async function handleModelConfig(agentId: string) {
    modelConfigAgentId = agentId;
    modelConfigSaving = false;
    try {
      const result = await onGetAgentModelConfig(agentId);
      modelConfigProviderAlias = result.provider_alias || '';
      modelConfigSleepProvider = result.sleep_provider_alias || '';
      modelConfigProvidersList = result.providers || [];
      showModelConfigPopup = true;
    } catch (error) {
      console.error('Failed to load agent model config:', error);
    }
  }

  async function saveModelConfig() {
    modelConfigSaving = true;
    try {
      await onSaveAgentModelConfig(modelConfigAgentId, {
        provider_alias: modelConfigProviderAlias,
        sleep_provider_alias: modelConfigSleepProvider || null,
      });
      showModelConfigPopup = false;
    } catch (error) {
      console.error('Failed to save agent model config:', error);
    } finally {
      modelConfigSaving = false;
    }
  }

  // Type definitions
  type NodeStatus = {
    node_id: string;
    dpc_uris?: Array<{ ip: string; uri: string }>;
    external_uris?: Array<{ ip: string; uri: string }>;
    connected_to_hub?: boolean;
    hub_url?: string;
    hub_status?: string;
    operation_mode?: string;
    connection_status?: string;
    available_features?: Record<string, boolean>;
    peer_info?: Array<{ node_id: string; name?: string }>;
    p2p_peers?: string[];
    cached_peers_count?: number;
  };

  type ChatInfo = {
    name: string;
    provider?: string;
    instruction_set_name?: string;
    profile_name?: string;
    llm_provider?: string;
  };

  type AgentInfo = {
    agent_id: string;
    name: string;
    provider_alias: string;
    profile_name: string;
    instruction_set_name?: string;
    created_at: string;
    updated_at?: string;
    // Telegram integration fields (v0.22.0+)
    telegram_enabled?: boolean;
    telegram_bot_token?: string;
    telegram_allowed_chat_ids?: string[];
    telegram_event_filter?: string[];
    telegram_max_events_per_minute?: number;
    telegram_cooldown_seconds?: number;
    telegram_transcription_enabled?: boolean;
    telegram_unified_conversation?: boolean;
    telegram_linked_at?: string;
    // Legacy field (deprecated in favor of telegram_allowed_chat_ids)
    telegram_chat_id?: string;
  };

  // Props (Svelte 5 runes mode)
  let {
    connectionStatus,
    nodeStatus,
    aiChats,
    unreadMessageCounts,
    activeChatId = $bindable(),
    peerDisplayNames,
    peerInput = $bindable(""),
    isConnecting,
    peersByStrategy,
    formatPeerForTooltip,

    // Event handlers
    onReconnect,
    onLoginToHub,
    onViewPersonalContext,
    onOpenInstructionsEditor,
    onOpenFirewallEditor,
    onOpenProvidersEditor,
    onOpenAgentBoard,
    onConnectPeer,
    onResetUnreadCount,
    onGetPeerDisplayName,
    onAddAIChat,
    onAddAgentChat,
    onDeleteAIChat,
    onDisconnectPeer,
    groupChats = new Map(),
    onCreateGroup,
    onLeaveGroup,
    onDeleteGroup,
    selfNodeId = "",
    // Agent list (Phase 4)
    agents = [],
    onSelectAgent,
    onDeleteAgent,
    onLinkAgentTelegram,
    onUnlinkAgentTelegram,
    onGetAgentModelConfig,
    onSaveAgentModelConfig,
  }: {
    connectionStatus: string;
    nodeStatus: NodeStatus | null;
    aiChats: Map<string, ChatInfo>;
    unreadMessageCounts: Map<string, number>;
    activeChatId?: string;
    peerDisplayNames: Map<string, string>;
    peerInput?: string;
    isConnecting: boolean;
    peersByStrategy: Record<string, any[]>;
    formatPeerForTooltip: (peer: any) => string;
    onReconnect: () => void;
    onLoginToHub: (provider: string) => void;
    onViewPersonalContext: () => void;
    onOpenInstructionsEditor: () => void;
    onOpenFirewallEditor: () => void;
    onOpenProvidersEditor: () => void;
    onOpenAgentBoard?: () => void;
    onConnectPeer: () => void;
    onResetUnreadCount: (peerId: string) => void;
    onGetPeerDisplayName: (peerId: string) => string;
    onAddAIChat: () => void;
    onAddAgentChat?: () => void;
    onDeleteAIChat: (chatId: string) => void;
    onDisconnectPeer: (peerId: string) => void;
    groupChats?: Map<string, any>;
    onCreateGroup?: () => void;
    onLeaveGroup?: (groupId: string) => void;
    onDeleteGroup?: (groupId: string) => void;
    selfNodeId?: string;
    // Agent list (Phase 4)
    agents?: AgentInfo[];
    onSelectAgent?: (agentId: string) => void;
    onDeleteAgent?: (agentId: string) => void;
    onLinkAgentTelegram?: (agentId: string, config: {
      bot_token: string;
      chat_ids: string[];
      event_filter?: string[];
      max_events_per_minute?: number;
      cooldown_seconds?: number;
      transcription_enabled?: boolean;
      unified_conversation?: boolean;
    }) => Promise<void>;
    onUnlinkAgentTelegram?: (agentId: string) => Promise<void>;
    onGetAgentModelConfig: (agentId: string) => Promise<any>;
    onSaveAgentModelConfig: (agentId: string, config: { provider_alias: string; sleep_provider_alias: string | null }) => Promise<void>;
  } = $props();
</script>

<div class="sidebar">
  <!-- Status Bar (only shown when NOT connected) -->
  {#if connectionStatus !== 'connected'}
    <div class="status-bar">
      {#if connectionStatus === 'connecting'}
        <span class="status-connecting">Backend status: connecting...</span>
      {:else if connectionStatus === 'error'}
        <span class="status-error">Backend status: error</span>
        <button class="btn-small" onclick={onReconnect}>Retry</button>
      {:else}
        <span class="status-disconnected">Backend status: disconnected</span>
        <button class="btn-small" onclick={onReconnect}>Connect</button>
      {/if}
    </div>
  {/if}
  {#if connectionStatus === 'connected' && nodeStatus}
    <!-- Node Info -->
    <div class="node-info">
      <p><strong>Your Node ID:</strong></p>
      <div class="node-id-container">
        <code class="node-id">{nodeStatus.node_id}</code>
        <button
          class="copy-btn"
          onclick={() => {
            navigator.clipboard.writeText(nodeStatus.node_id);
            alert('Node ID copied!');
          }}
          title="Copy Node ID"
        >
          📋
        </button>
      </div>

      <!-- Direct TLS Connection URIs (Local Network) -->
      {#if nodeStatus.dpc_uris && nodeStatus.dpc_uris.length > 0}
        <div class="dpc-uris-section">
          <details class="uri-details">
            <summary class="uri-summary">
              <span class="uri-title">Local Network ({nodeStatus.dpc_uris.length})</span>
            </summary>
            <div class="uri-help-text">
              Share with peers on your local network
            </div>
            {#each nodeStatus.dpc_uris as { ip, uri }}
              <div class="uri-card">
                <div class="uri-card-header">
                  <span class="ip-badge">{ip}</span>
                  <button
                    class="copy-btn-icon"
                    onclick={() => {
                      navigator.clipboard.writeText(uri);
                      alert('✓ URI copied!');
                    }}
                    title="Copy URI"
                  >
                    📋
                  </button>
                </div>
                <details class="uri-full-details">
                  <summary class="show-uri-btn">Full URI ▼</summary>
                  <code class="uri-full-text">{uri}</code>
                </details>
              </div>
            {/each}
          </details>
        </div>
      {/if}

      <!-- External URIs (From STUN Servers) -->
      {#if nodeStatus.external_uris && nodeStatus.external_uris.length > 0}
        <div class="dpc-uris-section">
          <details class="uri-details">
            <summary class="uri-summary">
              <span class="uri-title">External (Internet) ({nodeStatus.external_uris.length})</span>
            </summary>
            <div class="uri-help-text">
              Your public IP address(es) discovered via STUN servers
            </div>
            {#each nodeStatus.external_uris as { ip, uri }}
              <div class="uri-card">
                <div class="uri-card-header">
                  <span class="ip-badge external">{ip}</span>
                  <button
                    class="copy-btn-icon"
                    onclick={() => {
                      navigator.clipboard.writeText(uri);
                      alert('✓ External URI copied!');
                    }}
                    title="Copy External URI"
                  >
                    📋
                  </button>
                </div>
                <details class="uri-full-details">
                  <summary class="show-uri-btn">Full URI ▼</summary>
                  <code class="uri-full-text">{uri}</code>
                </details>
              </div>
            {/each}
          </details>
        </div>
      {/if}

      <!-- Hub Mode (separate section) -->
      {#if nodeStatus.operation_mode}
        <div class="dpc-uris-section">
          <details class="uri-details">
            <summary class="uri-summary">
              <span class="uri-title">Hub Mode</span>
            </summary>

            <div class="hub-mode-content">
              <div
                class="mode-badge"
                class:fully-online={nodeStatus.operation_mode === 'fully_online'}
                class:hub-offline={nodeStatus.operation_mode === 'hub_offline'}
                class:fully-offline={nodeStatus.operation_mode === 'fully_offline'}
              >
                {#if nodeStatus.operation_mode === 'fully_online'}
                  🟢 Online
                {:else if nodeStatus.operation_mode === 'hub_offline'}
                  🟡 Hub Offline
                {:else}
                  🔴 Offline
                {/if}
              </div>
              <p class="mode-description">
                {#if nodeStatus.connection_status}
                  {nodeStatus.connection_status}
                {:else}
                  All features available
                {/if}
              </p>

              <!-- OAuth Login (when Hub disconnected) -->
              {#if nodeStatus.hub_status !== 'Connected' && !nodeStatus.connected_to_hub}
                <div class="hub-login-section">
                  <p class="info-text">Connect to Hub for WebRTC signaling</p>
                  <div class="oauth-buttons">
                    <button
                      onclick={() => onLoginToHub('google')}
                      class="btn-oauth btn-google"
                      title="Login with Google"
                    >
                      <span class="oauth-icon">🔵</span>
                      Google
                    </button>
                    <button
                      onclick={() => onLoginToHub('github')}
                      class="btn-oauth btn-github"
                      title="Login with GitHub"
                    >
                      <span class="oauth-icon">⚫</span>
                      GitHub
                    </button>
                  </div>
                </div>
              {/if}
            </div>
          </details>
        </div>
      {/if}
    </div>

    <!-- Personal Context Button (Knowledge Architecture) -->
    <div class="context-section">
      <button class="btn-context" onclick={onViewPersonalContext}>
        View Personal Context
      </button>

      <button class="btn-context" onclick={onOpenInstructionsEditor}>
        AI Instructions
      </button>

      <button class="btn-context" onclick={onOpenFirewallEditor}>
        Firewall and Privacy Rules
      </button>

      <button class="btn-context" onclick={onOpenProvidersEditor}>
        AI Providers
      </button>

      {#if onOpenAgentBoard}
        <button class="btn-context" onclick={onOpenAgentBoard}>
          Agent Progress Board
        </button>
      {/if}

    </div>

    <!-- Connect to Peer -->
    <div class="connect-section">
      <h3>Connect to Peer</h3>
      <input
        id="peer-input"
        name="peer-input"
        type="text"
        bind:value={peerInput}
        placeholder="node_id or dpc://IP:port?node_id=..."
        onkeydown={(e) => e.key === 'Enter' && onConnectPeer()}
      />
      <button
        onclick={onConnectPeer}
        disabled={isConnecting || !peerInput.trim()}
      >
        {isConnecting ? '🔄 Connecting...' : 'Connect'}
      </button>

      <!-- Connection Methods Help (Collapsible) -->
      <details class="connection-methods-details">
        <summary class="connection-methods-summary">
          <span class="uri-icon">ℹ️</span>
          <span class="uri-title">Connection Methods</span>
        </summary>
        <div class="connection-help-content">
          <p class="small">
            🔍 <strong>Auto-Discovery (DHT):</strong> <code>dpc-node-abc123...</code><br/>
            <span class="small-detail">Tries: DHT → Cache → IPv4 → Hub → Relay → Gossip</span>
          </p>
          <p class="small">
            🏠 <strong>Direct TLS (Local):</strong> <code>dpc://192.168.1.100:8888?node_id=...</code>
          </p>
          <p class="small">
            🌍 <strong>Direct TLS (External):</strong> <code>dpc://203.0.113.5:8888?node_id=...</code>
          </p>
        </div>
      </details>

      <!-- Available Features -->
      {#if nodeStatus.available_features}
        <details class="connection-methods-details" style="margin-top: 0.75rem;">
          <summary class="connection-methods-summary">
            <span class="uri-icon">ℹ️</span>
            <span class="uri-title">Available Features</span>
          </summary>
          <div class="connection-help-content">
            <ul class="features-list">
              {#each Object.entries(nodeStatus.available_features) as [feature, available]}
                {@const peerCount = peersByStrategy[feature]?.length || 0}
                {@const tooltip = peersByStrategy[feature]
                  ? peersByStrategy[feature].map(formatPeerForTooltip).join(', ')
                  : ''}
                <li
                  class:feature-available={available}
                  class:feature-unavailable={!available}
                  title={peerCount > 0 ? tooltip : ''}
                >
                  {available ? '✓' : '✗'} {feature.replace(/_/g, ' ')}
                  {#if peerCount > 0}
                    <span class="peer-count">({peerCount})</span>
                  {/if}
                </li>
              {/each}
            </ul>
            {#if nodeStatus.cached_peers_count && nodeStatus.cached_peers_count > 0}
              <p class="cached-info">💾 {nodeStatus.cached_peers_count} cached peer(s)</p>
            {/if}
          </div>
        </details>
      {/if}
    </div>

    <!-- Chat List -->
    <div class="chat-list">
      <div class="chat-list-header">
        <h3>Chats</h3>
        <div class="chat-buttons">
          <button
            class="btn-add-chat"
            onclick={onAddAIChat}
            title="Add a new AI chat with a different provider"
          >
            + AI
          </button>
          {#if onAddAgentChat}
            <button
              class="btn-add-chat btn-add-agent"
              onclick={onAddAgentChat}
              title="Start a new chat with the embedded DPC Agent"
            >
              + Agent
            </button>
          {/if}
        </div>
      </div>
      <ul>
        <!-- Agent List (Phase 4) -->
        {#if agents && agents.length > 0}
          <li class="section-divider"><span>Agents</span></li>
          {#each agents as agent (agent.agent_id)}
            <li class="peer-item">
              <button
                type="button"
                class="chat-button agent-button"
                class:active={activeChatId === agent.agent_id}
                onclick={() => {
                  if (onSelectAgent) {
                    onSelectAgent(agent.agent_id);
                  }
                }}
                title="{agent.name} (Profile: {agent.profile_name}, LLM: {agent.provider_alias})"
              >
                <span class="agent-name">{agent.name}</span>
                <span
                  role="button"
                  tabindex="0"
                  class="agent-provider"
                  title="Click to configure models"
                  onclick={(e) => { e.stopPropagation(); handleModelConfig(agent.agent_id); }}
                  onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); handleModelConfig(agent.agent_id); } }}
                >{agent.provider_alias}</span>
                {#if agent.telegram_enabled}
                  <span
                    role="button"
                    tabindex="0"
                    class="telegram-link-badge"
                    title="Linked to Telegram — click to edit settings"
                    onclick={(e) => { e.stopPropagation(); handleLinkTelegram(agent.agent_id); }}
                    onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); handleLinkTelegram(agent.agent_id); } }}
                  >✓ 📱</span>
                {/if}
              </button>
              <div class="agent-actions">
                {#if !agent.telegram_enabled && onLinkAgentTelegram}
                  <button
                    type="button"
                    class="telegram-action-btn link-btn"
                    onclick={(e) => { e.stopPropagation(); handleLinkTelegram(agent.agent_id); }}
                    title="Link to Telegram"
                  >
                    📱+
                  </button>
                {/if}
                {#if onDeleteAgent}
                  <button
                    type="button"
                    class="disconnect-btn"
                    onclick={(e) => { e.stopPropagation(); onDeleteAgent(agent.agent_id); }}
                    title="Delete agent"
                  >
                    ×
                  </button>
                {/if}
              </div>
            </li>
          {/each}
        {/if}

        <!-- AI Chats (excludes DPC Agents which appear in Agents section above) -->
        {#each Array.from(aiChats.entries()).filter(([_, chatInfo]) => chatInfo.provider !== 'dpc_agent') as [chatId, chatInfo] (chatId)}
          <li class="peer-item">
            <button
              type="button"
              class="chat-button"
              class:active={activeChatId === chatId}
              class:telegram-chat={chatInfo.provider === 'telegram'}
              onclick={() => {
                activeChatId = chatId;
                onResetUnreadCount(chatId);
              }}
              title={chatInfo.provider ? `Provider: ${chatInfo.provider}` : 'Default AI Assistant'}
            >
              {chatInfo.name}
              {#if (unreadMessageCounts.get(chatId) ?? 0) > 0}
                <span class="unread-badge">{unreadMessageCounts.get(chatId)}</span>
              {/if}
            </button>
            {#if chatId !== 'local_ai'}
              <button
                type="button"
                class="disconnect-btn"
                onclick={(e) => { e.stopPropagation(); onDeleteAIChat(chatId); }}
                title={chatInfo.provider === 'telegram' ? 'Delete Telegram chat' : 'Delete AI chat'}
              >
                ×
              </button>
            {/if}
          </li>
        {/each}

        <!-- P2P Peer Chats -->
        {#if nodeStatus.p2p_peers && nodeStatus.p2p_peers.length > 0}
          {#each nodeStatus.p2p_peers as peerId (`${peerId}-${peerDisplayNames.get(peerId)}`)}
            <li class="peer-item">
              <button
                type="button"
                class="chat-button"
                class:active={activeChatId === peerId}
                onclick={() => {
                  activeChatId = peerId;
                  onResetUnreadCount(peerId);
                }}
                title={peerId}
              >
                <span class="peer-name">👤 {onGetPeerDisplayName(peerId)}</span>
                {#if (unreadMessageCounts.get(peerId) ?? 0) > 0}
                  <span class="unread-badge">{unreadMessageCounts.get(peerId)}</span>
                {/if}
              </button>
              <button
                type="button"
                class="disconnect-btn"
                onclick={(e) => { e.stopPropagation(); onDisconnectPeer(peerId); }}
                title="Disconnect from peer"
              >
                ×
              </button>
            </li>
          {/each}
        {:else}
          <li class="no-peers">No connected peers</li>
        {/if}

        <!-- Group Chats (v0.19.0) -->
        {#if groupChats.size > 0}
          <li class="section-divider"><span>Group Chats</span></li>
          {#each [...groupChats.values()] as group (group.group_id)}
            <li class="peer-item">
              <button
                type="button"
                class="chat-button"
                class:active={activeChatId === group.group_id}
                onclick={() => {
                  activeChatId = group.group_id;
                  onResetUnreadCount(group.group_id);
                }}
                title="{group.name} ({group.members?.length || 0} members)"
              >
                <span class="peer-name">{group.name}</span>
                <span class="member-count">{group.members?.length || 0}</span>
                {#if (unreadMessageCounts.get(group.group_id) ?? 0) > 0}
                  <span class="unread-badge">{unreadMessageCounts.get(group.group_id)}</span>
                {/if}
              </button>
              <button
                type="button"
                class="disconnect-btn"
                onclick={(e) => {
                  e.stopPropagation();
                  if (group.created_by === selfNodeId) {
                    onDeleteGroup?.(group.group_id);
                  } else {
                    onLeaveGroup?.(group.group_id);
                  }
                }}
                title={group.created_by === selfNodeId ? "Delete group" : "Leave group"}
              >
                ×
              </button>
            </li>
          {/each}
        {/if}

        <!-- + Group Button (always visible, disabled when no peers) -->
        <li class="add-group-item">
          <button
            type="button"
            class="add-group-btn"
            onclick={() => onCreateGroup?.()}
            title="Create new group chat"
          >
            + Group
          </button>
        </li>
      </ul>
    </div>
  {:else if connectionStatus === 'connecting'}
    <div class="connecting">
      <p>🔄 Connecting...</p>
    </div>
  {:else}
    <div class="error">
      <p>⚠️ Not connected to Core Service</p>
      <p class="small">Please ensure the backend is running</p>
    </div>
  {/if}
</div>

<!-- Telegram Link Dialog -->
{#if showModelConfigPopup}
  <div class="telegram-link-dialog-overlay" role="presentation" onkeydown={(e) => e.key === 'Escape' && (showModelConfigPopup = false)}>
    <div class="telegram-link-dialog" role="dialog" aria-modal="true" aria-labelledby="model-config-title">
      <div class="dialog-header">
        <h3 id="model-config-title">Agent Models Configuration</h3>
        <button type="button" class="dialog-close-btn" onclick={() => showModelConfigPopup = false} aria-label="Close dialog">&times;</button>
      </div>
      <div class="dialog-content">
        <div class="existing-link-info">
          <p class="dialog-info">
            Configure LLM providers for this agent. Each agent can use a different model for chat and sleep consolidation.
          </p>
        </div>
        <hr class="dialog-divider">

        <label for="main-llm" class="dialog-label">Agent Main LLM:</label>
        <select id="main-llm" class="dialog-input" bind:value={modelConfigProviderAlias}>
          {#each modelConfigProvidersList as p}
            <option value={p.alias}>{p.alias} ({p.model})</option>
          {/each}
        </select>
        <p class="dialog-hint">Primary language model used for agent conversations.</p>

        <label for="sleep-llm" class="dialog-label">Sleep feature LLM:</label>
        <select id="sleep-llm" class="dialog-input" bind:value={modelConfigSleepProvider}>
          <option value="">Default (global)</option>
          {#each modelConfigProvidersList as p}
            <option value={p.alias}>{p.alias} ({p.model})</option>
          {/each}
        </select>
        <p class="dialog-hint">Model used for sleep consolidation analysis. "Default" uses the global provider.</p>
      </div>
      <div class="dialog-actions">
        <button type="button" class="btn-cancel" onclick={() => showModelConfigPopup = false}>Cancel</button>
        <button type="button" class="btn-save" onclick={saveModelConfig} disabled={modelConfigSaving}>
          {modelConfigSaving ? 'Saving...' : 'Save'}
        </button>
      </div>
    </div>
  </div>
{/if}

{#if showTelegramLinkDialog}
  <div class="telegram-link-dialog-overlay" role="presentation" onkeydown={(e) => e.key === 'Escape' && cancelTelegramLink()}>
    <div class="telegram-link-dialog" role="dialog" aria-modal="true" aria-labelledby="telegram-dialog-title">
      <div class="dialog-header">
        <h3 id="telegram-dialog-title">
          {agents.find(a => a.agent_id === linkingAgentId)?.telegram_enabled ? 'Edit Telegram Configuration' : 'Link Agent to Telegram'}
        </h3>
        <button
          type="button"
          class="dialog-close-btn"
          onclick={cancelTelegramLink}
          aria-label="Close dialog"
        >
          ×
        </button>
      </div>
      <div class="dialog-content">
        {#if agents.find(a => a.agent_id === linkingAgentId)?.telegram_enabled}
          <div class="existing-link-info">
            <p class="dialog-info">
              ✓ This agent is already linked to Telegram with {agents.find(a => a.agent_id === linkingAgentId)?.telegram_allowed_chat_ids?.length || 0} chat(s)
            </p>
            <p class="dialog-info small">
              Linked at: {agents.find(a => a.agent_id === linkingAgentId)?.telegram_linked_at || 'Unknown'}
            </p>
          </div>
          <hr class="dialog-divider">
          <p class="dialog-info">
            Update the configuration below or click "Unlink" to remove Telegram integration.
          </p>
        {:else}
          <p class="dialog-info">
            Configure Telegram integration for this agent. You can find your chat ID by messaging
            <a href="https://t.me/userinfobot" target="_blank" rel="noopener noreferrer">@userinfobot</a>
            on Telegram.
            <br><br>
            Create a bot via <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">@BotFather</a>
            to get a bot token.
          </p>
        {/if}

        <!-- Bot Token -->
        <label for="telegram-bot-token" class="dialog-label">
          Bot Token:
        </label>
        <input
          id="telegram-bot-token"
          type="password"
          bind:value={telegramBotToken}
          placeholder="123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
          onkeydown={(e) => e.key === 'Enter' && confirmTelegramLink()}
          class="dialog-input"
        />

        <!-- Allowed Chat IDs -->
        <label for="telegram-chat-ids" class="dialog-label">
          Allowed Chat IDs (comma-separated):
        </label>
        <input
          id="telegram-chat-ids"
          type="text"
          bind:value={telegramAllowedChatIds}
          placeholder="123456789, 987654321"
          onkeydown={(e) => e.key === 'Enter' && confirmTelegramLink()}
          class="dialog-input"
        />
        <p class="dialog-info small">
          Enter multiple chat IDs separated by commas. Use negative IDs for groups.
        </p>

        <!-- Event Filter -->
        <span class="dialog-label">Event Filter:</span>
        <div class="event-filter-grid">
          {#each ALL_EVENT_TYPES as evt}
            <label class="event-filter-item">
              <input
                type="checkbox"
                checked={isEventSelected(evt.key)}
                onchange={() => toggleEventType(evt.key)}
              />
              {evt.label}
            </label>
          {/each}
        </div>

        <!-- Rate Limiting -->
        <div class="form-row">
          <div class="form-col">
            <label for="telegram-max-events" class="dialog-label">
              Max Events/Minute:
            </label>
            <input
              id="telegram-max-events"
              type="number"
              bind:value={telegramMaxEventsPerMinute}
              min="1"
              max="100"
              class="dialog-input"
            />
          </div>
          <div class="form-col">
            <label for="telegram-cooldown" class="dialog-label">
              Cooldown (seconds):
            </label>
            <input
              id="telegram-cooldown"
              type="number"
              bind:value={telegramCooldownSeconds}
              min="0"
              max="60"
              step="0.5"
              class="dialog-input"
            />
          </div>
        </div>

        <!-- Transcription -->
        <label class="dialog-label checkbox-label">
          <input
            type="checkbox"
            bind:checked={telegramTranscriptionEnabled}
          />
          Enable voice message transcription
        </label>

        <!-- Unified conversation -->
        <label class="dialog-label checkbox-label">
          <input
            type="checkbox"
            bind:checked={telegramUnifiedConversation}
          />
          Share conversation history with DPC chat UI
        </label>
        <p class="dialog-hint">When enabled, messages sent via this Telegram bot will appear in the DPC chat panel and share the same conversation context.</p>

        {#if linkErrorMessage}
          <p class="dialog-error">{linkErrorMessage}</p>
        {/if}

        <div class="dialog-actions">
          <button
            type="button"
            class="dialog-btn dialog-btn-cancel"
            onclick={cancelTelegramLink}
          >
            Cancel
          </button>
          {#if agents.find(a => a.agent_id === linkingAgentId)?.telegram_enabled}
            <button
              type="button"
              class="dialog-btn dialog-btn-unlink"
              onclick={handleUnlinkFromDialog}
            >
              Unlink
            </button>
          {/if}
          <button
            type="button"
            class="dialog-btn dialog-btn-confirm"
            onclick={confirmTelegramLink}
            disabled={!telegramBotToken.trim() || !telegramAllowedChatIds.trim()}
          >
            {agents.find(a => a.agent_id === linkingAgentId)?.telegram_enabled ? 'Update Configuration' : 'Link Agent'}
          </button>
        </div>
      </div>
    </div>
  </div>
{/if}

<style>
  .sidebar {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    overflow-y: auto;
    max-height: calc(100vh - 8rem);
    padding-right: 0.5rem;
  }

  .sidebar::-webkit-scrollbar {
    width: 6px;
  }

  .sidebar::-webkit-scrollbar-track {
    background: #f1f1f1;
    border-radius: 3px;
  }

  .sidebar::-webkit-scrollbar-thumb {
    background: #888;
    border-radius: 3px;
  }

  .sidebar::-webkit-scrollbar-thumb:hover {
    background: #555;
  }

  .status-bar {
    margin-bottom: 1rem;
    padding: 0.75rem;
    border: 1px solid #ccc;
    border-radius: 6px;
    background: #f9f9f9;
    text-align: center;
    font-size: 0.9rem;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.75rem;
  }

  .status-disconnected, .status-error { color: #dc3545; font-weight: bold; }
  .status-connecting { color: #ffc107; font-weight: bold; }

  .btn-small {
    padding: 0.4rem 0.8rem;
    font-size: 0.9rem;
    border: none;
    border-radius: 6px;
    background: #007bff;
    color: white;
    cursor: pointer;
  }

  .node-info, .connect-section, .context-section, .chat-list {
    background: white;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 1rem;
  }

  /* Note: .node-info no longer has max-height restriction to prevent
     unwanted scrollbars on macOS/Ubuntu. Content naturally fits in sidebar. */

  h3 {
    margin: 0 0 0.75rem 0;
    font-size: 1.1rem;
    border-bottom: 1px solid #eee;
    padding-bottom: 0.5rem;
  }

  /* Connection Methods Collapsible Section */
  .connection-methods-details {
    margin-top: 0.75rem;
    background: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 0;
    overflow: hidden;
  }

  .connection-methods-details[open] {
    border-color: #007bff;
  }

  .connection-methods-summary {
    padding: 0.75rem 1rem;
    background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
    cursor: pointer;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 600;
    color: #333;
    transition: background 0.2s;
  }

  .connection-methods-summary:hover {
    background: linear-gradient(135deg, #e9ecef 0%, #dee2e6 100%);
  }

  .connection-methods-summary::-webkit-details-marker {
    display: none;
  }

  .connection-help-content {
    padding: 1rem;
    background: #ffffff;
  }

  .connection-help-content p {
    margin: 0.5rem 0;
  }

  .node-id {
    font-family: monospace;
    font-size: 0.85rem;
    color: #555;
    word-break: break-all;
    margin: 0;
  }

  .node-id-container {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
  }

  .copy-btn {
    width: auto;
    min-width: auto;
    padding: 0.3rem 0.5rem;
    font-size: 1rem;
    background: transparent;
    border: 1px solid #ddd;
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.2s;
  }

  .copy-btn:hover {
    background: #f0f0f0;
    border-color: #007bff;
  }

  .small {
    font-size: 0.9rem;
    color: #666;
    margin: 0.5rem 0;
    font-style: italic;
  }

  /* OAuth login buttons */
  .btn-oauth {
    flex: 1;
    min-width: 120px;
    padding: 0.75rem 1rem;
    border: none;
    border-radius: 4px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
  }

  .btn-oauth:hover {
    transform: translateY(-1px);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
  }

  .btn-oauth:active {
    transform: translateY(0);
  }

  .btn-google {
    background: #4285f4;
    color: white;
  }

  .btn-google:hover {
    background: #357ae8;
  }

  .btn-github {
    background: #24292e;
    color: white;
  }

  .btn-github:hover {
    background: #1b1f23;
  }

  .oauth-icon {
    font-size: 1.1rem;
    line-height: 1;
  }

  .oauth-buttons {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  /* Knowledge Architecture - Context Button */
  .btn-context {
    width: 100%;
    padding: 0.75rem 1rem;
    margin-bottom: 0.5rem;
    background: #5a67d8;
    color: white;
    border: none;
    border-radius: 6px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    box-shadow: 0 2px 8px rgba(90, 103, 216, 0.3);
  }

  .btn-context:hover {
    background: #4c51bf;
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(90, 103, 216, 0.4);
  }

  .btn-context:active {
    transform: translateY(0);
    box-shadow: 0 1px 4px rgba(90, 103, 216, 0.2);
  }

  .chat-list-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .chat-list-header h3 {
    margin: 0;
    padding: 0;
    border: none;
  }

  .btn-add-chat {
    padding: 0.3rem 0.6rem;
    background: #4CAF50;
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.75rem;
    font-weight: 600;
    transition: all 0.2s;
    box-shadow: 0 2px 4px rgba(76, 175, 80, 0.3);
    white-space: nowrap;
    flex-shrink: 0;
    width: fit-content;
    min-width: auto;
  }

  .btn-add-chat:hover {
    background: #45a049;
    box-shadow: 0 4px 8px rgba(76, 175, 80, 0.4);
    transform: translateY(-1px);
  }

  .btn-add-chat:active {
    transform: translateY(0);
    box-shadow: 0 1px 3px rgba(76, 175, 80, 0.2);
  }

  .chat-buttons {
    display: flex;
    gap: 0.5rem;
  }

  .btn-add-agent {
    background: #9333ea;
    box-shadow: 0 2px 4px rgba(147, 51, 234, 0.3);
  }

  .btn-add-agent:hover {
    background: #7c3aed;
    box-shadow: 0 4px 8px rgba(147, 51, 234, 0.4);
  }

  .chat-list ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }

  .chat-list li {
    margin-bottom: 0.5rem;
  }

  .peer-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .chat-button {
    display: flex;
    align-items: center;
    text-align: left;
    background: transparent;
    color: #333;
    border: 1px solid transparent;
    padding: 0.6rem;
    transition: all 0.2s;
    flex: 1;
    position: relative;
  }

  .chat-button:hover {
    background: #f0f0f0;
  }

  .chat-button.active {
    background: #e0e7ff;
    border-color: #c7d2fe;
    font-weight: bold;
  }

  /* Telegram chat styling (v0.14.0+) */
  .chat-button.telegram-chat {
    background: linear-gradient(135deg, #e6f3ff 0%, #cce5ff 100%);
    border-color: #99ccff;
    color: #006699;
  }

  .chat-button.telegram-chat:hover {
    background: linear-gradient(135deg, #cce5ff 0%, #b3d9ff 100%);
    border-color: #66b3ff;
  }

  .chat-button.telegram-chat.active {
    background: linear-gradient(135deg, #b3e0ff 0%, #99d6ff 100%);
    border-color: #3399ff;
    font-weight: bold;
  }

  /* Peer name wrapper (v0.9.3) - handles overflow so badge stays visible */
  .peer-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }

  /* Unread message badge (v0.9.3) */
  .unread-badge {
    display: inline-block;
    background: #dc3545;
    color: white;
    font-size: 0.7rem;
    font-weight: bold;
    padding: 0.15rem 0.4rem;
    border-radius: 10px;
    margin-left: 0.5rem;
    min-width: 1.2rem;
    text-align: center;
  }

  .disconnect-btn {
    padding: 0.3rem 0.6rem;
    background: transparent;
    color: #999;
    font-size: 1.5rem;
    border: 1px solid transparent;
    flex: 1;
  }

  .disconnect-btn:hover {
    background: #ffebee;
    color: #dc3545;
  }

  /* Agent button styles (Phase 4) */
  .agent-button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
  }

  .agent-button .agent-name {
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-button .agent-provider {
    font-size: 0.65rem;
    color: #6c7086;
    background: #313244;
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    margin-left: 0.25rem;
    cursor: pointer;
    transition: background 0.2s;
  }

  .agent-button .agent-provider:hover {
    background: #45475a;
    color: #cdd6f4;
  }

  .no-peers {
    text-align: center;
    color: #999;
    font-style: italic;
    padding: 1rem;
  }

  .section-divider {
    list-style: none;
    padding: 8px 12px 4px;
    font-size: 0.7rem;
    color: #6c7086;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-weight: 600;
  }

  .member-count {
    font-size: 0.7rem;
    color: #6c7086;
    margin-left: 4px;
  }

  .add-group-item {
    list-style: none;
    padding: 2px 8px;
  }

  .add-group-btn {
    width: 100%;
    padding: 6px 12px;
    border: 1px dashed #45475a;
    border-radius: 6px;
    background: transparent;
    color: #6c7086;
    cursor: pointer;
    font-size: 0.8rem;
    transition: all 0.15s;
  }

  .add-group-btn:hover:not(:disabled) {
    border-color: #89b4fa;
    color: #89b4fa;
    background: rgba(137, 180, 250, 0.05);
  }

  .add-group-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .cached-info {
    font-size: 0.8rem;
    color: #666;
    margin-top: 0.5rem;
    padding: 0.4rem;
    background: #f0f0f0;
    border-radius: 4px;
    text-align: center;
  }

  /* DPC URI Styles - Redesigned for better UX */
  .dpc-uris-section {
    margin-top: 1rem;
    margin-bottom: 0.5rem;
  }

  .uri-details {
    background: #ffffff;
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 0;
    overflow: hidden;
  }

  .uri-details[open] {
    border-color: #007bff;
  }

  .uri-summary {
    padding: 0.75rem 1rem;
    background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
    cursor: pointer;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 600;
    color: #495057;
    transition: background 0.2s;
    user-select: none;
  }

  .uri-summary::-webkit-details-marker {
    display: none;
  }

  .uri-summary:hover {
    background: linear-gradient(135deg, #e9ecef 0%, #dee2e6 100%);
  }

  .uri-icon {
    font-size: 1.2rem;
  }

  .uri-title {
    flex: 1;
    font-size: 0.9rem;
  }

  .uri-help-text {
    padding: 0.5rem 1rem 0.75rem;
    font-size: 0.75rem;
    color: #6c757d;
    background: #f8f9fa;
    border-bottom: 1px solid #e9ecef;
  }

  .uri-card {
    padding: 0.75rem 1rem;
    border-bottom: 1px solid #f0f0f0;
    transition: background 0.2s;
  }

  .uri-card:last-of-type {
    border-bottom: none;
  }

  .uri-card:hover {
    background: #f8f9fa;
  }

  .uri-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .ip-badge {
    flex: 1;
    font-family: 'Courier New', monospace;
    font-size: 0.9rem;
    font-weight: 600;
    color: #0056b3;
    padding: 0.4rem 0.6rem;
    background: #e7f1ff;
    border-radius: 6px;
    border: 1px solid #b3d7ff;
  }

  .ip-badge.external {
    color: #0d6e2b;
    background: #d1f4e0;
    border: 1px solid #7fd99f;
  }

  .copy-btn-icon {
    width: auto;
    min-width: auto;
    padding: 0.3rem 0.5rem;
    font-size: 1rem;
    background: transparent;
    border: 1px solid #ddd;
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.2s;
    flex-shrink: 0;
  }

  .copy-btn-icon:hover {
    background: #f0f0f0;
    border-color: #007bff;
  }

  .copy-btn-icon:active {
    background: #e0e0e0;
  }

  .uri-full-details {
    margin-top: 0.5rem;
  }

  .uri-full-details summary {
    display: none;
  }

  .show-uri-btn {
    display: inline-block !important;
    font-size: 0.75rem;
    color: #007bff;
    cursor: pointer;
    padding: 0.3rem 0.6rem;
    background: #f0f7ff;
    border: 1px solid #cce5ff;
    border-radius: 4px;
    margin-top: 0.4rem;
    transition: all 0.2s;
    user-select: none;
  }

  .show-uri-btn:hover {
    background: #e0f0ff;
    border-color: #99ccff;
  }

  .uri-full-text {
    display: block;
    margin-top: 0.5rem;
    padding: 0.6rem;
    font-family: 'Courier New', monospace;
    font-size: 0.7rem;
    color: #495057;
    background: #f8f9fa;
    border: 1px solid #dee2e6;
    border-radius: 4px;
    word-break: break-all;
    line-height: 1.5;
  }

  .hub-login-section {
    padding: 0.75rem;
    background: #ffffff;
    border-radius: 8px;
  }

  .hub-mode-content {
    padding: 1rem;
  }

  .mode-badge {
    display: inline-block;
    padding: 0.5rem 1rem;
    border-radius: 6px;
    font-weight: 600;
    font-size: 0.9rem;
    margin-bottom: 0.75rem;
  }

  .mode-badge.fully-online {
    background: #d4edda;
    color: #155724;
    border: 1px solid #c3e6cb;
  }

  .mode-badge.hub-offline {
    background: #fff3cd;
    color: #856404;
    border: 1px solid #ffeaa7;
  }

  .mode-badge.fully-offline {
    background: #f8d7da;
    color: #721c24;
    border: 1px solid #f5c6cb;
  }

  .mode-description {
    font-size: 0.85rem;
    color: #666;
    margin: 0 0 1rem 0;
    font-style: italic;
  }

  .info-text {
    font-size: 0.85rem;
    color: #666;
    margin: 0.5rem 0;
  }

  .features-list {
    list-style: none;
    padding: 0.5rem 0;
    margin: 0;
  }

  .features-list li {
    padding: 0.5rem 1rem;
    font-size: 0.85rem;
    border-bottom: 1px solid #f0f0f0;
  }

  .features-list li:last-child {
    border-bottom: none;
  }

  .feature-available {
    color: #28a745;
  }

  .feature-unavailable {
    color: #dc3545;
  }

  .peer-count {
    color: #666;
    font-size: 0.75rem;
    margin-left: 0.5rem;
    font-weight: normal;
  }

  .small-detail {
    font-size: 0.75rem;
    color: #999;
    display: block;
    margin-top: 0.2rem;
  }

  button {
    width: 100%;
    padding: 0.75rem;
    border: none;
    border-radius: 6px;
    background: #007bff;
    color: white;
    font-size: 1rem;
    cursor: pointer;
    transition: background 0.2s;
  }

  button:hover:not(:disabled) {
    background: #0056b3;
  }

  button:disabled {
    background: #a0a0a0;
    cursor: not-allowed;
  }

  input {
    width: 100%;
    padding: 0.75rem;
    border: 1px solid #ccc;
    border-radius: 6px;
    font-size: 1rem;
    font-family: inherit;
    box-sizing: border-box;
    margin-bottom: 0.5rem;
  }

  /* Agent Actions */
  .agent-actions {
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }

  .telegram-action-btn {
    padding: 0.3rem 0.5rem;
    background: transparent;
    color: #0088cc;
    font-size: 0.9rem;
    border: 1px solid #0088cc;
    flex: 0 0 auto;
    min-width: auto;
    width: auto;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s;
  }

  .telegram-action-btn:hover:not(:disabled) {
    background: #e6f3ff;
    transform: translateY(-1px);
  }

  .telegram-action-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .telegram-link-badge {
    font-size: 0.65rem;
    background: #0088cc;
    color: white;
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    margin-left: 0.25rem;
    border: none;
    cursor: pointer;
    line-height: 1;
  }
  .telegram-link-badge:hover {
    background: #0077b6;
  }

  /* Telegram Link Dialog */
  .telegram-link-dialog-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 1rem;
  }

  .telegram-link-dialog {
    background: white;
    border-radius: 8px;
    max-width: 680px;
    width: 100%;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.15);
  }

  .dialog-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.5rem;
    border-bottom: 1px solid #e0e0e0;
  }

  .dialog-header h3 {
    margin: 0;
    padding: 0;
    border: none;
    font-size: 1.1rem;
  }

  .dialog-close-btn {
    background: transparent;
    border: none;
    font-size: 1.5rem;
    color: #666;
    cursor: pointer;
    padding: 0;
    width: auto;
    min-width: auto;
    line-height: 1;
  }

  .dialog-close-btn:hover {
    background: transparent;
    color: #333;
  }

  .dialog-content {
    padding: 1.5rem;
  }

  .dialog-info {
    font-size: 0.9rem;
    color: #666;
    margin-bottom: 1rem;
    line-height: 1.5;
  }

  .dialog-info a {
    color: #0088cc;
    text-decoration: none;
  }

  .dialog-info a:hover {
    text-decoration: underline;
  }

  .dialog-label {
    display: block;
    font-weight: 600;
    margin-bottom: 0.5rem;
    color: #333;
  }

  .dialog-input {
    width: 100%;
    padding: 0.75rem;
    border: 1px solid #ccc;
    border-radius: 6px;
    font-size: 1rem;
    font-family: inherit;
    box-sizing: border-box;
    margin-bottom: 0.5rem;
  }

  .dialog-error {
    color: #dc3545;
    font-size: 0.85rem;
    margin: -0.25rem 0 0.5rem 0;
  }

  .dialog-hint {
    color: #888;
    font-size: 0.8rem;
    margin: -0.25rem 0 0.5rem 0;
    line-height: 1.4;
  }

  .event-filter-grid {
    display: flex;
    flex-wrap: wrap;
    margin-bottom: 0.75rem;
    padding: 0.5rem;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 4px;
  }

  .event-filter-item {
    width: 50%;
    box-sizing: border-box;
    padding: 0.2rem 0.5rem 0.2rem 0;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.8rem;
    color: #333;
    cursor: pointer;
    white-space: nowrap;
  }

  .event-filter-item input[type="checkbox"] {
    width: auto;
    flex-shrink: 0;
    cursor: pointer;
  }

  .dialog-actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 1rem;
  }

  .dialog-btn {
    flex: 1;
    padding: 0.75rem 1rem;
    border: none;
    border-radius: 6px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s;
  }

  .dialog-btn-cancel {
    background: #e0e0e0;
    color: #333;
  }

  .dialog-btn-cancel:hover {
    background: #d0d0d0;
  }

  .dialog-btn-confirm {
    background: #0088cc;
    color: white;
  }

  .dialog-btn-confirm:hover:not(:disabled) {
    background: #0077b3;
  }

  .dialog-btn-confirm:disabled {
    background: #a0c4e8;
    cursor: not-allowed;
  }

  .dialog-btn-unlink {
    background: #d32f2f;
    color: white;
  }

  .dialog-btn-unlink:hover {
    background: #c62828;
  }

  /* New form elements for expanded Telegram dialog */
  .existing-link-info {
    background: #e8f5e9;
    padding: 0.75rem;
    border-radius: 6px;
    margin-bottom: 1rem;
  }

  .existing-link-info .dialog-info {
    margin: 0;
  }

  .dialog-divider {
    border: none;
    border-top: 1px solid #e0e0e0;
    margin: 1rem 0;
  }

  .form-row {
    display: flex;
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .form-col {
    flex: 1;
  }

  .form-col .dialog-label {
    margin-bottom: 0.25rem;
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 600;
    margin-bottom: 1rem;
    cursor: pointer;
  }

  .checkbox-label input[type="checkbox"] {
    width: 1.2rem;
    height: 1.2rem;
    cursor: pointer;
  }

  .small {
    font-size: 0.85rem;
    color: #666;
  }

  .dialog-info.small {
    font-size: 0.85rem;
    color: #666;
    margin-top: 0.25rem;
  }
</style>
