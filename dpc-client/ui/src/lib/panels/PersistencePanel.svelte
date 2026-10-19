<!-- src/lib/panels/PersistencePanel.svelte -->
<!-- Loads the AI chats and Telegram chats from the backend on connect, after a one-time import of what the old UI kept in localStorage -->
<!-- Logic-only panel — no markup, no styles. -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import { connectionStatus, sendCommand, telegramLinkedChats } from '$lib/coreService';
  import {
    readLegacyChatStore,
    importUnits,
    importPayloads,
    writeImportRemainder,
    sidebarEntry,
    legacyImportPending,
    FINAL_IMPORT_STATUSES,
    IMPORT_PAYLOAD_LIMIT,
    type AIChatMeta,
    type ImportOutcome,
  } from '$lib/utils/aiChatStore';

  // ---------------------------------------------------------------------------
  // Props
  // ---------------------------------------------------------------------------
  let {
    aiChats,
    chatProviders,
  }: {
    aiChats: Writable<Map<string, AIChatMeta>>;
    chatProviders: Writable<Map<string, string>>;
  } = $props();

  // ---------------------------------------------------------------------------
  // One-time import (2026-10-19). Until then this panel wrote the chat list and
  // the AI chat histories to localStorage, outside ~/.dpc, and +page restored
  // them at start without asking the backend. The backend owns both now; what
  // an older build left behind is handed to it once, chat by chat, and each
  // chat leaves the old store as soon as it has a final answer. The backend
  // imports a chat id once and never overwrites, so a chat asked again after a
  // reset or a delete stays reset or deleted.
  // ---------------------------------------------------------------------------
  async function importLegacyChats(): Promise<void> {
    const store = readLegacyChatStore(localStorage);
    if (!store) return;
    const units = importUnits(store);
    const outcomes: ImportOutcome[] = [];
    for (const unit of units) {
      const payloads = importPayloads(unit);
      if (!payloads) {
        // Sent whole it would close the socket, and the reconnect would send it
        // again. It stays in the old store; nothing else is held up by it.
        console.error(`[AI Chats] ${unit.conversation_id} has a message over ${IMPORT_PAYLOAD_LIMIT} bytes; not imported, kept in the old store`);
        outcomes.push({ unit, status: undefined });
        continue;
      }
      let status: string | undefined;
      for (const payload of payloads) {
        try {
          const result = await (sendCommand('import_ai_chats', payload) as Promise<any>);
          status = result?.results?.[0]?.status;
          if (status === 'refused' || (status && !FINAL_IMPORT_STATUSES.has(status))) {
            console.warn(`[AI Chats] Import of ${unit.conversation_id}: ${status} (${result.results[0].reason})`);
          }
        } catch (error) {
          console.error(`[AI Chats] Import of ${unit.conversation_id} did not answer:`, error);
          status = undefined;
        }
        if (status !== 'imported' && status !== 'kept') break;  // a later part cannot fix an earlier one
      }
      outcomes.push({ unit, status });
    }
    let removed: string[] = [];
    try {
      removed = writeImportRemainder(localStorage, store, outcomes);
    } catch (error) {
      console.error('[AI Chats] Could not shrink the old store; it is asked again next start:', error);
    }
    const open = outcomes.filter(o => !o.status || !FINAL_IMPORT_STATUSES.has(o.status)).length;
    console.log(`[AI Chats] Import from the old UI store: ${units.length - open} of ${units.length} chat(s) settled; removed ${removed.join(', ') || 'no key'}`);
  }

  async function loadChatsFromBackend(): Promise<void> {
    try {
      await importLegacyChats();
    } finally {
      // AI chat histories may be fetched now: whatever was imported is on disk.
      legacyImportPending.set(false);
    }
    const result = await (sendCommand('list_ai_chats', {}) as Promise<any>);
    if (result?.status !== 'success' || !Array.isArray(result.chats)) {
      console.error('[AI Chats] Could not list chats from the backend:', result?.message);
      return;
    }
    const entries = result.chats.map(sidebarEntry).filter((e: any): e is [string, AIChatMeta] => e !== null);
    // Added, never pruned: a chat the person created a moment ago is in the
    // map before its record is on disk. Removal is the delete path's job.
    aiChats.update(chats => {
      const next = new Map(chats);
      for (const [id, meta] of entries) if (!next.has(id)) next.set(id, meta);
      return next;
    });
    chatProviders.update(map => {
      const next = new Map(map);
      for (const [id, meta] of entries) {
        if (meta.provider && meta.provider !== 'telegram' && !next.has(id)) next.set(id, meta.provider);
      }
      return next;
    });
    const telegramLinks = result.chats.filter((c: any) => c.provider === 'telegram' && c.telegram_chat_id);
    if (telegramLinks.length > 0) {
      telegramLinkedChats.update(links => {
        const next = new Map(links);
        for (const c of telegramLinks) next.set(c.conversation_id, String(c.telegram_chat_id));
        return next;
      });
    }
    console.log(`[AI Chats] Loaded ${entries.length} chat(s) from the backend`);
  }

  // Once per connection: a backend restart while the window stays open lists again.
  let loadedForConnection = false;
  $effect(() => {
    if ($connectionStatus !== 'connected') {
      loadedForConnection = false;
      return;
    }
    if (loadedForConnection) return;
    loadedForConnection = true;
    loadChatsFromBackend().catch(error => console.error('[AI Chats] Loading chats failed:', error));
  });
</script>

<!-- No markup — logic-only panel -->
