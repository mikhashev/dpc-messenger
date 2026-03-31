<!-- src/lib/panels/AddAIChatPanel.svelte -->
<!-- Add AI Chat dialog + handlers (Phase 3 Step 8 continuation) -->
<!-- Has markup: modal dialog for creating AI chats / agent chats -->
<!-- Owns: showAddAIChatDialog, all dialog field state -->
<!-- Exports: open(), openForAgent() -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import {
    availableProviders,
    peerProviders,
    nodeStatus,
    telegramLinkedChats,
    telegramMessages,
    createAgent,
    listAgentProfiles,
    sendCommand,
  } from '$lib/coreService';

  // ---------------------------------------------------------------------------
  // Types
  // ---------------------------------------------------------------------------
  type AIChatMeta = {
    name: string;
    provider: string;
    instruction_set_name?: string;
    profile_name?: string;
    llm_provider?: string;
    compute_host?: string;
  };

  type InstructionSets = {
    schema_version: string;
    default: string;
    sets: Record<string, { name: string; description: string }>;
  };

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    aiChats,
    chatHistories,
    chatProviders,
    availableInstructionSets,
    onSetActiveChatId,
    onSetAgentChatToAgentId,
    onAgentToast,
  }: {
    aiChats: Writable<Map<string, AIChatMeta>>;
    chatHistories: Writable<Map<string, any[]>>;
    chatProviders: Writable<Map<string, string>>;
    availableInstructionSets: InstructionSets | null;
    onSetActiveChatId: (chatId: string) => void;
    onSetAgentChatToAgentId: (chatId: string, agentId: string) => void;
    onAgentToast: (message: string, type: 'info' | 'warning' | 'error') => void;
  } = $props();

  // ---------------------------------------------------------------------------
  // State (all owned here)
  // ---------------------------------------------------------------------------
  let showAddAIChatDialog = $state(false);
  let selectedProviderForNewChat = $state('');
  let selectedInstructionSetForNewChat = $state('general');
  let selectedProfileForNewAgent = $state('default');
  let newAgentName = $state('');
  let selectedAgentLLMProvider = $state('');
  let selectedDialogComputeHost = $state('local');
  let availableAgentProfiles = $state<string[]>(['default']);

  // ---------------------------------------------------------------------------
  // Public API (called from +page.svelte via bind:this)
  // ---------------------------------------------------------------------------

  export async function open() {
    if (!$availableProviders || !$availableProviders.providers || $availableProviders.providers.length === 0) {
      alert('No AI providers available. Please configure providers in ~/.dpc/providers.toml');
      return;
    }

    try {
      const profilesResult = await listAgentProfiles();
      if (profilesResult?.status === 'success' && profilesResult.profiles) {
        availableAgentProfiles = profilesResult.profiles;
      }
    } catch (e) {
      console.warn('Failed to load agent profiles:', e);
      availableAgentProfiles = ['default'];
    }

    selectedProviderForNewChat = $availableProviders.default_provider;
    selectedInstructionSetForNewChat = availableInstructionSets?.default || 'general';
    selectedProfileForNewAgent = 'default';
    showAddAIChatDialog = true;
  }

  export async function openForAgent() {
    const agentProvider = $availableProviders?.providers?.find((p: any) => p.alias === 'dpc_agent');
    if (!agentProvider) {
      alert("DPC Agent provider not configured. Add 'dpc_agent' to ~/.dpc/providers.json");
      return;
    }

    try {
      const profilesResult = await listAgentProfiles();
      if (profilesResult?.status === 'success' && profilesResult.profiles) {
        availableAgentProfiles = profilesResult.profiles;
      }
    } catch (e) {
      console.warn('Failed to load agent profiles:', e);
      availableAgentProfiles = ['default'];
    }

    selectedProviderForNewChat = 'dpc_agent';
    selectedInstructionSetForNewChat = availableInstructionSets?.default || 'general';
    selectedProfileForNewAgent = 'default';
    showAddAIChatDialog = true;
  }

  // ---------------------------------------------------------------------------
  // Handlers
  // ---------------------------------------------------------------------------

  async function confirmAddAIChat() {
    if (!selectedProviderForNewChat) return;

    const dialogProviders = selectedDialogComputeHost === 'local'
      ? $availableProviders.providers
      : ($peerProviders.get(selectedDialogComputeHost) ?? []);
    const provider = dialogProviders.find((p: any) => p.alias === selectedProviderForNewChat);
    if (!provider) {
      alert(`Provider '${selectedProviderForNewChat}' not found.`);
      return;
    }

    let chatName: string;
    if (selectedProviderForNewChat === 'dpc_agent') {
      chatName = newAgentName.trim() || `Agent (${selectedProfileForNewAgent})`;
    } else {
      chatName = `${provider.alias} (${provider.model})`;
    }

    let chatId = `ai_chat_${crypto.randomUUID().slice(0, 8)}`;

    if (selectedProviderForNewChat === 'dpc_agent') {
      try {
        const llmProviderAlias = selectedAgentLLMProvider || $availableProviders?.default_provider || 'dpc_agent';
        const llmProviderList = selectedDialogComputeHost === 'local'
          ? $availableProviders.providers
          : ($peerProviders.get(selectedDialogComputeHost) ?? []);
        const llmProviderInfo = llmProviderList.find((p: any) => p.alias === llmProviderAlias);
        const result = await createAgent(
          chatName,
          llmProviderAlias,
          selectedProfileForNewAgent,
          'general',
          50.0, 200,
          selectedDialogComputeHost !== 'local' ? selectedDialogComputeHost : undefined,
          llmProviderInfo?.context_window
        );
        if (result?.status === 'success') {
          console.log('[DPC Agent] Created agent storage:', result.agent_id);
          chatId = result.agent_id;
          onSetAgentChatToAgentId(chatId, chatId);
          onAgentToast(`Agent "${chatName}" created successfully`, 'info');
        } else {
          console.warn('[DPC Agent] Failed to create agent storage:', result?.message);
          onAgentToast(`Warning: Agent chat created but storage failed: ${result?.message}`, 'warning');
        }
      } catch (e) {
        console.warn('[DPC Agent] Error creating agent storage:', e);
      }
    }

    aiChats.update(chats => {
      const newMap = new Map(chats);
      newMap.set(chatId, {
        name: chatName,
        provider: selectedProviderForNewChat,
        compute_host: selectedDialogComputeHost !== 'local' ? selectedDialogComputeHost : undefined,
        instruction_set_name: selectedProviderForNewChat === 'dpc_agent' ? 'general' : selectedInstructionSetForNewChat,
        profile_name: selectedProviderForNewChat === 'dpc_agent' ? selectedProfileForNewAgent : undefined,
        llm_provider: selectedProviderForNewChat === 'dpc_agent' ? selectedAgentLLMProvider : undefined,
      });
      return newMap;
    });

    chatProviders.update(map => {
      const newMap = new Map(map);
      newMap.set(chatId, selectedProviderForNewChat);
      return newMap;
    });

    chatHistories.update(h => {
      const newMap = new Map(h);
      newMap.set(chatId, []);
      return newMap;
    });

    onSetActiveChatId(chatId);
    showAddAIChatDialog = false;
  }

  function cancelAddAIChat() {
    showAddAIChatDialog = false;
    selectedProviderForNewChat = '';
    selectedProfileForNewAgent = 'default';
    newAgentName = '';
    selectedAgentLLMProvider = '';
    selectedDialogComputeHost = 'local';
  }

  export async function handleDeleteAIChat(chatId: string, ask: any) {
    if (chatId === 'local_ai') {
      if (ask) {
        await ask('Cannot delete the default Local AI chat.', { title: 'D-PC Messenger', kind: 'info' });
      } else {
        alert('Cannot delete the default Local AI chat.');
      }
      return;
    }

    let shouldDelete = false;
    if (ask) {
      if (chatId.startsWith('telegram-')) {
        shouldDelete = await ask(
          'Delete this Telegram chat? This will remove the chat history and unlink the Telegram conversation. You can still receive new messages from this contact.',
          { title: 'Confirm Telegram Chat Deletion', kind: 'warning' }
        );
      } else {
        shouldDelete = await ask(
          'Delete this AI chat? This will permanently remove the chat history.',
          { title: 'Confirm Deletion', kind: 'warning' }
        );
      }
    } else {
      if (chatId.startsWith('telegram-')) {
        shouldDelete = confirm('Delete this Telegram chat? This will remove the chat history and unlink the Telegram conversation. You can still receive new messages from this contact.');
      } else {
        shouldDelete = confirm('Delete this AI chat? This will permanently remove the chat history.');
      }
    }

    if (!shouldDelete) return;

    if (chatId.startsWith('telegram-')) {
      try {
        const result = await sendCommand('delete_telegram_conversation_link', { conversation_id: chatId });
        if (result.status === 'error') {
          console.error('Failed to delete Telegram conversation link:', result.message);
        }
      } catch (error) {
        console.error('Error deleting Telegram conversation link:', error);
      }
    }

    aiChats.update(chats => {
      const newMap = new Map(chats);
      newMap.delete(chatId);
      return newMap;
    });

    chatProviders.update(map => {
      const newMap = new Map(map);
      newMap.delete(chatId);
      return newMap;
    });

    chatHistories.update(h => {
      const newMap = new Map(h);
      newMap.delete(chatId);
      return newMap;
    });

    if (chatId.startsWith('telegram-')) {
      try {
        const savedTelegramChats = localStorage.getItem('dpc-telegram-chats');
        if (savedTelegramChats) {
          const telegramChats = JSON.parse(savedTelegramChats);
          delete telegramChats[chatId];
          localStorage.setItem('dpc-telegram-chats', JSON.stringify(telegramChats));
        }
      } catch (error) {
        console.error('[Telegram] Failed to update dpc-telegram-chats:', error);
      }

      telegramLinkedChats.update(links => {
        const newMap = new Map(links);
        newMap.delete(chatId);
        return newMap;
      });

      telegramMessages.update(msgs => {
        const newMap = new Map(msgs);
        newMap.delete(chatId);
        return newMap;
      });
    }

    console.log('AI chat deleted successfully');
    return chatId; // caller checks if activeChatId matches
  }
</script>

<style>
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    justify-content: center;
    align-items: center;
    z-index: 1000;
  }

  .modal-content {
    background: white;
    padding: 2rem;
    border-radius: 12px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
    max-width: 500px;
    width: 90%;
  }

  .modal-content h2 {
    margin: 0 0 0.5rem 0;
    color: #333;
    font-size: 1.5rem;
  }

  .modal-content p {
    margin: 0 0 1.5rem 0;
    color: #666;
    font-size: 0.95rem;
  }

  .dialog-provider-selector {
    margin-bottom: 1.5rem;
  }

  .dialog-provider-selector label {
    display: block;
    margin-bottom: 0.5rem;
    font-weight: 600;
    color: #333;
  }

  .dialog-provider-selector select {
    width: 100%;
    padding: 0.75rem;
    border: 1px solid #ddd;
    border-radius: 6px;
    font-size: 1rem;
    background: white;
    cursor: pointer;
  }

  .dialog-provider-selector select:hover { border-color: #4CAF50; }
  .dialog-provider-selector select:focus { outline: none; border-color: #4CAF50; box-shadow: 0 0 0 3px rgba(76, 175, 80, 0.1); }

  .dialog-actions {
    display: flex;
    gap: 1rem;
    justify-content: flex-end;
  }

  .btn-cancel,
  .btn-confirm {
    padding: 0.75rem 1.5rem;
    border: none;
    border-radius: 6px;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.2s;
  }

  .btn-cancel { background: #f0f0f0; color: #666; }
  .btn-cancel:hover { background: #e0e0e0; }
  .btn-confirm { background: #4CAF50; color: white; box-shadow: 0 2px 4px rgba(76, 175, 80, 0.3); }
  .btn-confirm:hover { background: #45a049; box-shadow: 0 4px 8px rgba(76, 175, 80, 0.4); transform: translateY(-1px); }
  .btn-confirm:active { transform: translateY(0); box-shadow: 0 1px 3px rgba(76, 175, 80, 0.2); }
</style>

<!-- Add AI Chat Dialog -->
{#if showAddAIChatDialog}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="modal-overlay"
    role="presentation"
    onclick={cancelAddAIChat}
    onkeydown={(e) => e.key === 'Escape' && cancelAddAIChat()}
  >
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="modal-content"
      role="dialog"
      aria-labelledby="modal-title"
      aria-modal="true"
      tabindex="-1"
      onclick={(e) => e.stopPropagation()}
    >
      <h2 id="modal-title">Add New AI Chat</h2>
      <p>Select an AI provider for the new chat:</p>

      {#if $nodeStatus?.peer_info && $nodeStatus.peer_info.length > 0}
        <div class="dialog-provider-selector">
          <label for="new-chat-ai-host">AI Host:</label>
          <select id="new-chat-ai-host" bind:value={selectedDialogComputeHost}>
            <option value="local">Local</option>
            {#each $nodeStatus.peer_info as peer}
              <option value={peer.node_id}>
                {peer.name ? `${peer.name} | ${peer.node_id.slice(0, 20)}...` : `${peer.node_id.slice(0, 20)}...`}
              </option>
            {/each}
          </select>
        </div>
      {/if}

      <div class="dialog-provider-selector">
        <label for="new-chat-provider">Chat Type:</label>
        <select id="new-chat-provider" bind:value={selectedProviderForNewChat}>
          {#each (selectedDialogComputeHost === 'local' ? $availableProviders.providers : ($peerProviders.get(selectedDialogComputeHost) ?? [])) as provider}
            <option value={provider.alias}>
              {#if provider.alias === 'dpc_agent'}
                DPC Agent (Autonomous AI with tools)
              {:else}
                {provider.alias} - {provider.model}
              {/if}
            </option>
          {/each}
        </select>
        <p class="dialog-hint" style="font-size: 0.85em; color: #888; margin-top: 4px;">
          {#if selectedProviderForNewChat === 'dpc_agent'}
            Agents are autonomous AI assistants with tool access (file system, web search, etc.)
          {:else}
            Standard AI chat using the selected provider
          {/if}
        </p>
      </div>

      {#if selectedProviderForNewChat === 'dpc_agent'}
        <div class="dialog-provider-selector">
          <label for="new-agent-name">Agent Name:</label>
          <input type="text" id="new-agent-name" bind:value={newAgentName} placeholder="e.g., Coding Assistant, Research Bot..." style="width: 100%; padding: 8px; border-radius: 4px; border: 1px solid #ccc;" />
        </div>

        <div class="dialog-provider-selector">
          <label for="new-chat-llm-provider">AI Model (LLM):</label>
          <select id="new-chat-llm-provider" bind:value={selectedAgentLLMProvider}>
            {#each (selectedDialogComputeHost === 'local' ? $availableProviders.providers : ($peerProviders.get(selectedDialogComputeHost) ?? [])) as provider}
              {#if provider.alias !== 'dpc_agent'}
                <option value={provider.alias}>
                  {provider.alias} - {provider.model}
                </option>
              {/if}
            {/each}
          </select>
          <p class="dialog-hint" style="font-size: 0.85em; color: #888; margin-top: 4px;">
            The underlying AI model this agent will use for reasoning.
          </p>
        </div>

        <div class="dialog-provider-selector">
          <label for="new-chat-profile">Permission Profile:</label>
          <select id="new-chat-profile" bind:value={selectedProfileForNewAgent}>
            {#each availableAgentProfiles as profile}
              <option value={profile}>{profile}</option>
            {/each}
          </select>
          <p class="dialog-hint" style="font-size: 0.85em; color: #888; margin-top: 4px;">
            Controls what tools and data this agent can access. Configure in Firewall → Agent Profiles.
          </p>
        </div>
      {:else}
        <div class="dialog-provider-selector">
          <label for="new-chat-instruction-set">Instruction Set:</label>
          <select id="new-chat-instruction-set" bind:value={selectedInstructionSetForNewChat}>
            <option value="none">None (No Instructions)</option>
            {#if availableInstructionSets}
              {#each Object.entries(availableInstructionSets.sets) as [key, set]}
                <option value={key}>
                  {set.name} {availableInstructionSets.default === key ? '⭐' : ''}
                </option>
              {/each}
            {:else}
              <option value="general">General Purpose</option>
            {/if}
          </select>
        </div>
      {/if}

      <div class="dialog-actions">
        <button class="btn-cancel" onclick={cancelAddAIChat}>Cancel</button>
        <button class="btn-confirm" onclick={confirmAddAIChat}>Create Chat</button>
      </div>
    </div>
  </div>
{/if}
