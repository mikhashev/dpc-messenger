<!-- src/lib/panels/PersistencePanel.svelte -->
<!-- Persists AI chats and chat histories to localStorage for page-refresh recovery -->
<!-- Logic-only panel — no markup, no styles. -->

<script lang="ts">
  import type { Writable } from 'svelte/store';

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    aiChats,
    chatHistories,
  }: {
    aiChats: Writable<Map<string, any>>;
    chatHistories: Writable<Map<string, any[]>>;
  } = $props();

  // ---------------------------------------------------------------------------
  // Internal state
  // ---------------------------------------------------------------------------
  let aiChatsSaveTimeout: ReturnType<typeof setTimeout> | null = null;
  let chatHistoriesSaveTimeout: ReturnType<typeof setTimeout> | null = null;

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // Persist AI chats (including Agent chats) to localStorage for page refresh recovery
  // Debounced to avoid excessive writes
  $effect(() => {
    $aiChats; // track reactively

    if (aiChatsSaveTimeout) clearTimeout(aiChatsSaveTimeout);

    aiChatsSaveTimeout = setTimeout(() => {
      try {
        const aiChatsToSave = Object.fromEntries(
          Array.from($aiChats.entries())
            .filter(([id, info]) =>
              (id.startsWith('ai_') || id.startsWith('agent_') || id === 'default') &&
              !id.startsWith('telegram-') &&
              info.provider !== 'telegram'
            )
        );
        localStorage.setItem('dpc-ai-chats', JSON.stringify(aiChatsToSave));
        console.log(`[AI Chats] Persisted ${Object.keys(aiChatsToSave).length} AI chats to localStorage`);
      } catch (error) {
        console.error('[AI Chats] Failed to persist chats:', error);
      }
    }, 500);
  });

  // Persist AI chat histories to localStorage for page refresh recovery
  // Debounced to avoid excessive writes
  // Agent chats are included so thinking blocks / raw tool-call output survive restarts.
  // On startup, backend messages are merged onto the localStorage snapshot so Telegram
  // messages received while the app was closed are picked up without losing UI metadata.
  $effect(() => {
    $chatHistories; // track reactively

    if (chatHistoriesSaveTimeout) clearTimeout(chatHistoriesSaveTimeout);

    chatHistoriesSaveTimeout = setTimeout(() => {
      try {
        const historiesToSave = Object.fromEntries(
          Array.from($chatHistories.entries())
            .filter(([id]) => (id.startsWith('ai_') || id.startsWith('agent_')) && !id.startsWith('telegram-'))
        );
        localStorage.setItem('dpc-ai-chat-histories', JSON.stringify(historiesToSave));
      } catch (error) {
        console.error('[AI Chats] Failed to persist chat histories:', error);
      }
    }, 500);
  });
</script>

<!-- No markup — logic-only panel -->
