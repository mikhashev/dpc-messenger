<!-- src/lib/panels/TelegramPanel.svelte -->
<!-- Telegram incoming-message effects panel (Phase 3 Step 8) -->
<!-- Logic-only panel — no markup, no styles. -->
<!-- Manages: $telegramMessageReceived, $telegramVoiceReceived, $telegramImageReceived, $telegramFileReceived effects -->

<script lang="ts">
  import type { Writable } from 'svelte/store';
  import {
    telegramMessageReceived,
    telegramVoiceReceived,
    telegramImageReceived,
    telegramFileReceived,
  } from '$lib/coreService';
  import { showNotificationIfBackground } from '$lib/notificationService';

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
  }: {
    aiChats: Writable<Map<string, AIChatMeta>>;
    chatHistories: Writable<Map<string, any[]>>;
  } = $props();

  // ---------------------------------------------------------------------------
  // Internal helper — auto-create Telegram conversation in aiChats + localStorage
  // ---------------------------------------------------------------------------
  function ensureTelegramChat(conversation_id: string, sender_name: string) {
    if ($aiChats.has(conversation_id) || conversation_id.startsWith('dpc-node-')) return;

    aiChats.update(chats => {
      const newMap = new Map(chats);
      newMap.set(conversation_id, {
        name: `📱 Telegram (${sender_name})`,
        provider: 'telegram',
        instruction_set_name: 'general'
      });
      return newMap;
    });
    console.log(`[Telegram] Auto-created chat ${conversation_id} in sidebar`);

    try {
      const telegramChats = Object.fromEntries(
        Array.from($aiChats.entries())
          .filter(([_, info]) => info.provider === 'telegram')
          .map(([id, info]) => [id, info])
      );
      localStorage.setItem('dpc-telegram-chats', JSON.stringify(telegramChats));
      console.log('[Telegram] Persisted Telegram chats to localStorage:', Object.keys(telegramChats));
    } catch (error) {
      console.error('[Telegram] Failed to persist chats:', error);
    }
  }

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // Handle Telegram text messages
  $effect(() => {
    if ($telegramMessageReceived) {
      const { conversation_id, telegram_chat_id, sender_name, text, timestamp } = $telegramMessageReceived;
      console.log(`[Telegram] Adding message to chat ${conversation_id}: ${text}`);

      ensureTelegramChat(conversation_id, sender_name);

      // B2 Fix 3: Skip append for agent conversations — agent_history_updated
      // already includes the Telegram message, so appending here causes duplicates
      // and race conditions with AgentPanel's history replacement.
      const isAgentChat = conversation_id.startsWith('agent_') || conversation_id.startsWith('agent-');
      if (!isAgentChat) {
        chatHistories.update(map => {
          const newMap = new Map(map);
          const currentMessages = newMap.get(conversation_id) || [];
          newMap.set(conversation_id, [
            ...currentMessages,
            {
              id: `telegram-${Date.now()}`,
              sender: `telegram-${telegram_chat_id}`,
              senderName: sender_name,
              text: text,
              timestamp: new Date(timestamp).getTime()
            }
          ]);
          return newMap;
        });
      }

      (async () => {
        const messagePreview = text.length > 50 ? text.slice(0, 50) + '...' : text;
        const notified = await showNotificationIfBackground({ title: sender_name, body: messagePreview });
        console.log(`[Notifications] Telegram message notification: ${notified ? 'system' : 'skip'}`);
      })();

      telegramMessageReceived.set(null);
    }
  });

  // Handle Telegram voice messages
  $effect(() => {
    if ($telegramVoiceReceived) {
      const { conversation_id, telegram_chat_id, sender_name, filename, file_path, duration_seconds, transcription } = $telegramVoiceReceived;
      console.log(`[Telegram] Adding voice message to chat ${conversation_id}: ${filename}`);

      ensureTelegramChat(conversation_id, sender_name);

      chatHistories.update(map => {
        const newMap = new Map(map);
        const currentMessages = newMap.get(conversation_id) || [];
        newMap.set(conversation_id, [
          ...currentMessages,
          {
            id: `telegram-voice-${Date.now()}`,
            sender: `telegram-${telegram_chat_id}`,
            senderName: sender_name,
            text: transcription ? `Voice message: ${transcription}` : 'Voice message',
            timestamp: Date.now(),
            attachments: [{
              type: 'voice',
              filename: filename,
              file_path: file_path,
              size_bytes: 0,
              mime_type: 'audio/ogg',
              voice_metadata: {
                duration_seconds: duration_seconds,
                sample_rate: 48000,
                channels: 1,
                codec: 'opus',
                recorded_at: new Date().toISOString()
              },
              transcription: transcription ? { text: transcription, provider: 'unknown' } : undefined
            }]
          }
        ]);
        return newMap;
      });

      (async () => {
        const notified = await showNotificationIfBackground({
          title: sender_name,
          body: `🎤 Voice message (${duration_seconds}s)`
        });
        console.log(`[Notifications] Telegram voice notification: ${notified ? 'system' : 'skip'}`);
      })();

      telegramVoiceReceived.set(null);
    }
  });

  // Handle Telegram image messages
  $effect(() => {
    const imageEvent = $telegramImageReceived;
    if (imageEvent) {
      const { conversation_id, telegram_chat_id, sender_name, filename, file_path, caption, size_bytes } = imageEvent;
      console.log(`[Telegram] Adding image to chat ${conversation_id}: ${filename}`);

      ensureTelegramChat(conversation_id, sender_name);

      chatHistories.update(map => {
        const newMap = new Map(map);
        const currentMessages = newMap.get(conversation_id) || [];
        newMap.set(conversation_id, [
          ...currentMessages,
          {
            id: `telegram-image-${Date.now()}`,
            sender: `telegram-${telegram_chat_id}`,
            senderName: sender_name,
            text: caption || "Image",
            timestamp: Date.now(),
            attachments: [{
              type: 'image',
              filename: filename,
              file_path: file_path,
              size_bytes: size_bytes || 0
            }]
          }
        ]);
        return newMap;
      });

      (async () => {
        const notified = await showNotificationIfBackground({
          title: sender_name,
          body: `📷 Photo${caption ? ': ' + caption.slice(0, 30) : ''}`
        });
        console.log(`[Notifications] Telegram image notification: ${notified ? 'system' : 'skip'}`);
      })();

      telegramImageReceived.set(null);
    }
  });

  // Handle Telegram file/document messages
  $effect(() => {
    const fileEvent = $telegramFileReceived;
    if (fileEvent) {
      const { conversation_id, telegram_chat_id, sender_name, filename, file_path, caption, size_bytes, mime_type } = fileEvent;
      console.log(`[Telegram] Adding file to chat ${conversation_id}: ${filename}`);

      ensureTelegramChat(conversation_id, sender_name);

      const isImage = mime_type?.startsWith('image/');

      chatHistories.update(map => {
        const newMap = new Map(map);
        const currentMessages = newMap.get(conversation_id) || [];
        newMap.set(conversation_id, [
          ...currentMessages,
          {
            id: `telegram-file-${Date.now()}`,
            sender: `telegram-${telegram_chat_id}`,
            senderName: sender_name,
            text: caption || filename,
            timestamp: Date.now(),
            attachments: [{
              type: isImage ? 'image' : 'file',
              filename: filename,
              file_path: file_path,
              size_bytes: size_bytes || 0,
              mime_type: mime_type
            }]
          }
        ]);
        return newMap;
      });

      (async () => {
        const notified = await showNotificationIfBackground({
          title: sender_name,
          body: `📎 File: ${filename}`
        });
        console.log(`[Notifications] Telegram file notification: ${notified ? 'system' : 'skip'}`);
      })();

      telegramFileReceived.set(null);
    }
  });
</script>

<!-- No markup — logic-only panel -->
