<!-- src/lib/panels/VoicePanel.svelte -->
<!-- Voice recording state and transcription effects panel (Phase 3 Step 6) -->
<!-- Logic-only panel — no markup, no styles. -->
<!-- Owns: autoTranscribeEnabled, whisperModelLoading, whisperModelLoadError -->
<!-- Manages: whisper loading events, voiceTranscription effects, loadAutoTranscribe effect -->

<script lang="ts">
  import type { Writable, Readable } from 'svelte/store';
  import {
    connectionStatus,
    voiceTranscriptionComplete,
    voiceTranscriptionReceived,
    whisperModelLoadingStarted,
    whisperModelLoaded,
    whisperModelLoadingFailed,
    setConversationTranscription,
    getConversationTranscription,
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
    activeChatId,
    aiChats,
    chatHistories,
    autoTranscribeEnabled = $bindable<boolean>(true),
    whisperModelLoading = $bindable<boolean>(false),
    whisperModelLoadError = $bindable<string | null>(null),
  }: {
    activeChatId: string;
    aiChats: Writable<Map<string, AIChatMeta>>;
    chatHistories: Writable<Map<string, any[]>>;
    autoTranscribeEnabled?: boolean;
    whisperModelLoading?: boolean;
    whisperModelLoadError?: string | null;
  } = $props();

  // ---------------------------------------------------------------------------
  // Exported functions (callable from +page.svelte via bind:this)
  // ---------------------------------------------------------------------------

  /** Save auto-transcribe setting for the current chat to the backend. */
  export async function saveAutoTranscribeSetting() {
    if ($aiChats.has(activeChatId) || activeChatId === 'local_ai') return;
    try {
      const result = await setConversationTranscription(activeChatId, autoTranscribeEnabled);
      console.log(`[AutoTranscribe] Saved setting for ${activeChatId}: ${autoTranscribeEnabled}`, result);
    } catch (error) {
      console.error(`[AutoTranscribe] Failed to save setting for ${activeChatId}:`, error);
    }
  }

  // ---------------------------------------------------------------------------
  // Internal helpers
  // ---------------------------------------------------------------------------

  async function loadAutoTranscribeSetting(chatId: string) {
    if ($aiChats.has(chatId) || chatId === 'local_ai') {
      autoTranscribeEnabled = true;
      return;
    }
    try {
      const result = await getConversationTranscription(chatId);
      if (result.status === 'success') {
        autoTranscribeEnabled = result.enabled;
        console.log(`[AutoTranscribe] Loaded setting for ${chatId}: ${autoTranscribeEnabled}`);
      } else {
        autoTranscribeEnabled = true;
        console.warn(`[AutoTranscribe] Failed to load setting for ${chatId}, defaulting to true`);
      }
    } catch (error) {
      console.error(`[AutoTranscribe] Error loading setting for ${chatId}:`, error);
      autoTranscribeEnabled = true;
    }
  }

  // ---------------------------------------------------------------------------
  // Effects
  // ---------------------------------------------------------------------------

  // Whisper model loading started
  $effect(() => {
    if ($whisperModelLoadingStarted) {
      console.log(`[Whisper] Model loading started: ${$whisperModelLoadingStarted.provider}`);
      whisperModelLoading = true;
      whisperModelLoadError = null;
    }
  });

  // Whisper model loaded successfully
  $effect(() => {
    if ($whisperModelLoaded) {
      console.log(`[Whisper] Model loaded successfully: ${$whisperModelLoaded.provider}`);
      whisperModelLoading = false;
      whisperModelLoadError = null;
    }
  });

  // Whisper model loading failed
  $effect(() => {
    if ($whisperModelLoadingFailed) {
      console.error(`[Whisper] Model loading failed: ${$whisperModelLoadingFailed.error}`);
      whisperModelLoading = false;
      whisperModelLoadError = $whisperModelLoadingFailed.error;
    }
  });

  // Load auto-transcribe setting when chat changes or connection is established
  $effect(() => {
    if (activeChatId && $connectionStatus === 'connected') {
      loadAutoTranscribeSetting(activeChatId);
    }
  });

  // Handle voice transcription complete (local transcription)
  $effect(() => {
    if ($voiceTranscriptionComplete) {
      const { transfer_id, text, transcriber_node_id, provider, confidence, language, timestamp, remote_provider_node_id } = $voiceTranscriptionComplete;
      console.log(`[VoiceTranscription] Received transcription for ${transfer_id}: "${text}"`);

      chatHistories.update(histories => {
        const updatedHistories = new Map();
        for (const [chatId, messages] of histories) {
          const updatedMessages = messages.map(message => {
            if (message.attachments) {
              const hasTargetVoice = message.attachments.some(
                (att: any) => att.type === 'voice' && att.transfer_id === transfer_id
              );
              if (hasTargetVoice) {
                return {
                  ...message,
                  attachments: message.attachments.map((attachment: any) => {
                    if (attachment.type === 'voice' && attachment.transfer_id === transfer_id) {
                      console.log(`[VoiceTranscription] Adding transcription to message in chat ${chatId}`);
                      return {
                        ...attachment,
                        transcription: { text, provider, transcriber_node_id, confidence, language, timestamp, remote_provider_node_id }
                      };
                    }
                    return attachment;
                  })
                };
              }
            }
            return message;
          });
          updatedHistories.set(chatId, updatedMessages);
        }
        return updatedHistories;
      });
    }
  });

  // Handle voice transcription received from peer
  $effect(() => {
    if ($voiceTranscriptionReceived) {
      const { transfer_id, text, transcriber_node_id, provider, confidence, language, timestamp } = $voiceTranscriptionReceived;
      console.log(`[VoiceTranscription] Received transcription from peer for ${transfer_id}: "${text}"`);

      chatHistories.update(histories => {
        const updatedHistories = new Map();
        for (const [chatId, messages] of histories) {
          const updatedMessages = messages.map(message => {
            if (message.attachments) {
              const hasTargetVoice = message.attachments.some(
                (att: any) => att.type === 'voice' && att.transfer_id === transfer_id
              );
              if (hasTargetVoice) {
                return {
                  ...message,
                  attachments: message.attachments.map((attachment: any) => {
                    if (attachment.type === 'voice' && attachment.transfer_id === transfer_id) {
                      console.log(`[VoiceTranscription] Adding peer transcription to message in chat ${chatId}`);
                      return {
                        ...attachment,
                        transcription: { text, provider, transcriber_node_id, confidence, language, timestamp }
                      };
                    }
                    return attachment;
                  })
                };
              }
            }
            return message;
          });
          updatedHistories.set(chatId, updatedMessages);
        }
        return updatedHistories;
      });
    }
  });
</script>

<!-- No markup — logic-only panel -->
