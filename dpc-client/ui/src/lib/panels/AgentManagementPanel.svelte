<!-- src/lib/panels/AgentManagementPanel.svelte -->
<!-- Agent select/delete/Telegram-link handlers (Phase 3 Step 8 continuation) -->
<!-- Logic-only panel — no markup, no styles. -->
<!-- Exports: handleSelectAgent(), handleDeleteAgent(), handleLinkAgentTelegram(), handleUnlinkAgentTelegram() -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import {
    agentsList,
    resetUnreadCount,
    sendCommand,
    deleteAgent,
    listAgents,
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

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    aiChats,
    chatHistories,
    chatProviders,
    onSetActiveChatId,
    onSetSelectedComputeHost,
    onSetAgentChatToAgentId,
    onAgentToast,
  }: {
    aiChats: Writable<Map<string, AIChatMeta>>;
    chatHistories: Writable<Map<string, any[]>>;
    chatProviders: Writable<Map<string, string>>;
    onSetActiveChatId: (chatId: string) => void;
    onSetSelectedComputeHost: (host: string) => void;
    onSetAgentChatToAgentId: (chatId: string, agentId: string) => void;
    onAgentToast: (message: string, type: 'info' | 'warning' | 'error') => void;
  } = $props();

  // ---------------------------------------------------------------------------
  // Public API (called from +page.svelte via bind:this)
  // ---------------------------------------------------------------------------

  export function handleSelectAgent(agentId: string, agentChatToAgentId: Map<string, string>, activeChatId: string) {
    console.log('Selected agent:', agentId);

    const agent = $agentsList.find((a: any) => a.agent_id === agentId);
    if (!agent) {
      console.error('Agent not found:', agentId);
      return;
    }

    // Check for existing chat mapped to this agent
    let existingChatId: string | null = null;
    for (const [chatId, mappedAgentId] of agentChatToAgentId) {
      if (mappedAgentId === agentId) {
        existingChatId = chatId;
        break;
      }
    }

    if (existingChatId && $aiChats.has(existingChatId)) {
      onSetActiveChatId(existingChatId);
      resetUnreadCount(existingChatId);
      console.log('Switched to existing agent chat:', existingChatId);
      return;
    }

    // Legacy: chat keyed directly by agentId
    if ($aiChats.has(agentId)) {
      onSetActiveChatId(agentId);
      resetUnreadCount(agentId);
      console.log('Switched to existing agent chat (legacy):', agentId);
      return;
    }

    // Create new chat for this agent
    aiChats.update(chats => {
      const newMap = new Map(chats);
      newMap.set(agentId, {
        name: agent.name,
        provider: 'dpc_agent',
        profile_name: agent.profile_name,
        llm_provider: agent.provider_alias,
        ...(agent.compute_host ? { compute_host: agent.compute_host } : {}),
      });
      return newMap;
    });

    onSetSelectedComputeHost(agent.compute_host || 'local');

    chatProviders.update(map => {
      const newMap = new Map(map);
      newMap.set(agentId, 'dpc_agent');
      return newMap;
    });

    onSetAgentChatToAgentId(agentId, agentId);
    onSetActiveChatId(agentId);
    console.log('Created new agent chat:', agentId);
  }

  export async function handleDeleteAgent(agentId: string, ask: any, activeChatId: string) {
    console.log('Delete agent:', agentId);

    let shouldDelete = false;
    if (ask) {
      shouldDelete = await ask(
        "Delete this agent? This will permanently remove the agent's memory, knowledge, and all associated data.",
        { title: 'Confirm Agent Deletion', kind: 'warning' }
      );
    } else {
      shouldDelete = confirm("Delete this agent? This will permanently remove the agent's memory, knowledge, and all associated data.");
    }

    if (!shouldDelete) return;

    try {
      const result = await deleteAgent(agentId);
      if (result.status === 'error') {
        console.error('Failed to delete agent:', result.message);
        onAgentToast(`Failed to delete agent: ${result.message}`, 'error');
        return;
      }

      const chatId = `agent_${agentId}`;
      if ($aiChats.has(chatId)) {
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
        if (activeChatId === chatId) {
          onSetActiveChatId('local_ai');
        }
      }

      onAgentToast('Agent deleted successfully', 'info');
      console.log('Agent deleted successfully');
    } catch (error) {
      console.error('Error deleting agent:', error);
      onAgentToast(`Error deleting agent: ${error}`, 'error');
    }
  }

  export async function handleLinkAgentTelegram(agentId: string, config: {
    bot_token: string;
    chat_ids: string[];
    event_filter?: string[];
    max_events_per_minute?: number;
    cooldown_seconds?: number;
    transcription_enabled?: boolean;
    unified_conversation?: boolean;
  }) {
    console.log('Link agent to Telegram:', agentId, 'config:', { ...config, bot_token: '***' });

    try {
      const result = await sendCommand('link_agent_telegram', {
        agent_id: agentId,
        bot_token: config.bot_token,
        chat_ids: config.chat_ids,
        event_filter: config.event_filter,
        max_events_per_minute: config.max_events_per_minute || 20,
        cooldown_seconds: config.cooldown_seconds || 3.0,
        transcription_enabled: config.transcription_enabled !== false,
        unified_conversation: config.unified_conversation === true,
      });

      if (result.status === 'error') {
        console.error('Failed to link agent to Telegram:', result.message);
        onAgentToast(`Failed to link agent: ${result.message}`, 'error');
        throw new Error(result.message);
      }

      onAgentToast('Agent Telegram configuration updated successfully', 'info');

      try {
        const agentsResult = await listAgents();
        if (agentsResult?.status === 'success' && agentsResult.agents) {
          agentsList.set(agentsResult.agents);
        }
      } catch (error) {
        console.error('Failed to refresh agents list:', error);
      }

      console.log('Agent Telegram configuration updated successfully');
    } catch (error) {
      console.error('Error linking agent to Telegram:', error);
      onAgentToast(`Error linking agent: ${error}`, 'error');
      throw error;
    }
  }

  export async function handleUnlinkAgentTelegram(agentId: string) {
    console.log('Unlink agent from Telegram:', agentId);

    try {
      const result = await sendCommand('unlink_agent_telegram', { agent_id: agentId });

      if (result.status === 'error') {
        console.error('Failed to unlink agent from Telegram:', result.message);
        onAgentToast(`Failed to unlink agent: ${result.message}`, 'error');
        return;
      }

      onAgentToast('Agent unlinked from Telegram successfully', 'info');

      try {
        const agentsResult = await listAgents();
        if (agentsResult?.status === 'success' && agentsResult.agents) {
          agentsList.set(agentsResult.agents);
        }
      } catch (error) {
        console.error('Failed to refresh agents list:', error);
      }

      console.log('Agent unlinked from Telegram successfully');
    } catch (error) {
      console.error('Error unlinking agent from Telegram:', error);
      onAgentToast(`Error unlinking agent: ${error}`, 'error');
    }
  }
</script>

<!-- No markup — logic-only panel -->
