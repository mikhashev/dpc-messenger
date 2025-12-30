<!-- Sidebar.svelte - Extracted sidebar navigation and connection management -->
<!-- Displays connection status, node info, peer list, chat list, and action buttons -->

<script lang="ts">
  // Type definitions
  type NodeStatus = {
    node_id: string;
    dpc_uris?: Array<{ ip: string; uri: string }>;
    external_uris?: Array<{ ip: string; uri: string }>;
    connected_to_hub?: boolean;
    hub_url?: string;
    operation_mode?: string;
    peer_info?: Array<{ node_id: string; name?: string }>;
    p2p_peers?: string[];
    cached_peers_count?: number;
  };

  type ChatInfo = {
    name: string;
    provider?: string;
    instruction_set_name?: string;
  };

  // Props (Svelte 5 runes mode)
  let {
    connectionStatus,
    nodeStatus,
    aiChats,
    unreadMessageCounts,
    activeChatId = $bindable(),
    peerDisplayNames,
    autoKnowledgeDetection = $bindable(false),
    peerInput = $bindable(""),
    isConnecting,

    // Event handlers
    onReconnect,
    onLoginToHub,
    onViewPersonalContext,
    onOpenInstructionsEditor,
    onOpenFirewallEditor,
    onOpenProvidersEditor,
    onToggleAutoKnowledgeDetection,
    onConnectPeer,
    onResetUnreadCount,
    onGetPeerDisplayName,
    onAddAIChat,
    onDeleteAIChat,
    onDisconnectPeer
  }: {
    connectionStatus: string;
    nodeStatus: NodeStatus | null;
    aiChats: Map<string, ChatInfo>;
    unreadMessageCounts: Map<string, number>;
    activeChatId?: string;
    peerDisplayNames: Map<string, string>;
    autoKnowledgeDetection?: boolean;
    peerInput?: string;
    isConnecting: boolean;
    onReconnect: () => void;
    onLoginToHub: (provider: string) => void;
    onViewPersonalContext: () => void;
    onOpenInstructionsEditor: () => void;
    onOpenFirewallEditor: () => void;
    onOpenProvidersEditor: () => void;
    onToggleAutoKnowledgeDetection: () => void;
    onConnectPeer: () => void;
    onResetUnreadCount: (peerId: string) => void;
    onGetPeerDisplayName: (peerId: string) => string;
    onAddAIChat: () => void;
    onDeleteAIChat: (chatId: string) => void;
    onDisconnectPeer: (peerId: string) => void;
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

      <!-- Hub Status (Collapsible) -->
      {#if nodeStatus.hub_url}
        <div class="dpc-uris-section">
          <details class="uri-details">
            <summary class="uri-summary">
              <span class="uri-icon">🌐</span>
              <span class="uri-title">
                Hub Status: {nodeStatus.connected_to_hub ? "✓ Connected" : "✗ Offline"}
              </span>
            </summary>
            <div class="hub-info-content">
              <p class="small"><strong>Hub URL:</strong></p>
              <code class="hub-url">{nodeStatus.hub_url}</code>

              {#if nodeStatus.operation_mode}
                <p class="small"><strong>Mode:</strong></p>
                <p class="mode-description">{nodeStatus.operation_mode}</p>
              {/if}

              {#if !nodeStatus.connected_to_hub}
                <div class="oauth-buttons">
                  <p class="small oauth-hint">Login to reconnect:</p>
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
              {/if}
            </div>
          </details>
        </div>
      {/if}

      <!-- Peer Discovery List (from DHT + Hub) -->
      {#if nodeStatus.peer_info && nodeStatus.peer_info.length > 0}
        <div class="dpc-uris-section">
          <details class="uri-details">
            <summary class="uri-summary">
              <span class="uri-icon">👥</span>
              <span class="uri-title">Discovered Peers ({nodeStatus.peer_info.length})</span>
            </summary>
            <ul class="peer-discovery-list">
              {#each nodeStatus.peer_info as peer}
                {@const displayName = peer.name
                  ? `${peer.name} | ${peer.node_id.slice(0, 20)}...`
                  : `${peer.node_id.slice(0, 20)}...`}
                {@const peerCount = nodeStatus.p2p_peers?.filter(id => id === peer.node_id).length || 0}
                <li class="peer-discovery-item">
                  {displayName}
                  {#if peerCount > 0}
                    <span class="peer-count">({peerCount})</span>
                  {/if}
                </li>
              {/each}
            </ul>
            {#if nodeStatus.cached_peers_count && nodeStatus.cached_peers_count > 0}
              <p class="cached-info">💾 {nodeStatus.cached_peers_count} cached peer(s)</p>
            {/if}
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

      <!-- Auto Knowledge Detection Toggle -->
      <div class="knowledge-toggle">
        <label class="toggle-container">
          <input
            id="auto-knowledge-detection"
            name="auto-knowledge-detection"
            type="checkbox"
            bind:checked={autoKnowledgeDetection}
            onchange={onToggleAutoKnowledgeDetection}
          />
          <span class="toggle-slider"></span>
          <span class="toggle-label">
            Auto-detect knowledge in conversations
          </span>
        </label>
        <p class="toggle-hint">
          {autoKnowledgeDetection
            ? "✓ AI is monitoring conversations for knowledge"
            : "✗ Manual knowledge extraction only"}
        </p>
      </div>
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
            <span class="small-detail">Tries: DHT → Cache → Hub</span>
          </p>
          <p class="small">
            🏠 <strong>Direct TLS (Local):</strong> <code>dpc://192.168.1.100:8888?node_id=...</code>
          </p>
          <p class="small">
            🌍 <strong>Direct TLS (External):</strong> <code>dpc://203.0.113.5:8888?node_id=...</code>
          </p>
        </div>
      </details>
    </div>

    <!-- Chat List -->
    <div class="chat-list">
      <div class="chat-list-header">
        <h3>Chats</h3>
        <button
          class="btn-add-chat"
          onclick={onAddAIChat}
          title="Add a new AI chat with a different provider"
        >
          + AI
        </button>
      </div>
      <ul>
        <!-- AI Chats -->
        {#each Array.from(aiChats.entries()) as [chatId, chatInfo] (chatId)}
          <li class="peer-item">
            <button
              type="button"
              class="chat-button"
              class:active={activeChatId === chatId}
              onclick={() => activeChatId = chatId}
              title={chatInfo.provider ? `Provider: ${chatInfo.provider}` : 'Default AI Assistant'}
            >
              {chatInfo.name}
            </button>
            {#if chatId !== 'local_ai'}
              <button
                type="button"
                class="disconnect-btn"
                onclick={(e) => { e.stopPropagation(); onDeleteAIChat(chatId); }}
                title="Delete AI chat"
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

  .oauth-hint {
    font-size: 0.85rem;
    color: #666;
    margin-bottom: 0.5rem;
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

  /* Knowledge Architecture - Auto-Detection Toggle */
  .knowledge-toggle {
    margin-top: 1rem;
    padding-top: 1rem;
    border-top: 1px solid #e0e0e0;
  }

  .toggle-container {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    cursor: pointer;
    user-select: none;
  }

  .toggle-container input[type="checkbox"] {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-slider {
    position: relative;
    width: 44px;
    height: 24px;
    background: #ccc;
    border-radius: 24px;
    transition: background 0.3s;
    flex-shrink: 0;
  }

  .toggle-slider::before {
    content: '';
    position: absolute;
    width: 18px;
    height: 18px;
    left: 3px;
    top: 3px;
    background: white;
    border-radius: 50%;
    transition: transform 0.3s;
  }

  .toggle-container input[type="checkbox"]:checked + .toggle-slider {
    background: #667eea;
  }

  .toggle-container input[type="checkbox"]:checked + .toggle-slider::before {
    transform: translateX(20px);
  }

  .toggle-label {
    font-size: 0.9rem;
    color: #333;
    line-height: 1.4;
  }

  .toggle-hint {
    font-size: 0.8rem;
    color: #666;
    margin: 0.5rem 0 0 0;
    padding-left: 3.5rem;
    line-height: 1.3;
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

  .no-peers {
    text-align: center;
    color: #999;
    font-style: italic;
    padding: 1rem;
  }

  .peer-count {
    color: #888;
    font-size: 0.9em;
    margin-left: 0.25rem;
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

  .hub-info-content {
    padding: 1rem;
  }

  .hub-url {
    font-family: monospace;
    font-size: 0.85rem;
    color: #555;
    word-break: break-all;
    margin: 0.5rem 0;
  }

  .mode-description {
    font-size: 0.85rem;
    color: #666;
    margin: 0.5rem 0;
    font-style: italic;
  }

  .peer-discovery-list {
    list-style: none;
    padding: 0.5rem 0;
    margin: 0;
  }

  .peer-discovery-item {
    padding: 0.5rem 1rem;
    font-size: 0.85rem;
    color: #333;
    border-bottom: 1px solid #f0f0f0;
  }

  .peer-discovery-item:last-child {
    border-bottom: none;
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
</style>
