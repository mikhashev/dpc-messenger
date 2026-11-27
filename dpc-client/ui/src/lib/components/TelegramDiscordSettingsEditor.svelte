<!-- TelegramDiscordSettingsEditor.svelte -->
<!-- Edit the Telegram and Discord keys of config.ini: the [telegram] and -->
<!-- [discord] enable switches and bot tokens, and the live [discord.guardrails] -->
<!-- and [discord.conversations] limits -->
<!-- (THE-TELEGRAM-AND-DISCORD-SETTINGS-CAN-ONLY-BE-TYPED-INTO-CONFIG-INI, -->
<!-- audit/config_ui_parity_2026-11-27.md). The switches and tokens are read once -->
<!-- at backend start — telegram_service.py builds the bot from one config read and -->
<!-- discord_service.py reads its config when it starts the bot — and those fields -->
<!-- say so; the limits are read as each Discord message arrives -->
<!-- (discord_coordinator.py) and need no restart. A stored token is never sent -->
<!-- back to this form: the field opens empty, empty keeps the stored token, and -->
<!-- only the Remove control clears it. -->

<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { sendCommand } from '$lib/coreService';
  import { boolValue, parseWholeNumber, tokenPlaceholder } from '$lib/utils/telegramDiscord';

  export let open: boolean = false;

  const dispatch = createEventDispatcher();

  // The drafts hold the switches as bools and the limits as text the save
  // parses (a number input binds a string until it holds a number); the token
  // fields are always empty on load — a stored token is never sent to the
  // form, only tokenSet, which the placeholder words.
  let tgDraft = { enabled: false, token: '', tokenSet: false };
  let dcDraft = {
    enabled: false,
    token: '',
    tokenSet: false,
    botTokenEnv: '',
    rate_limit_per_user_max: '10',
    rate_limit_per_user_window: '600',
    rate_limit_global_max: '60',
    rate_limit_global_window: '3600',
    response_delay_seconds: '3',
    ttl_minutes: '30',
    max_messages: '50',
  };

  let loading = false;
  let loadError = '';
  let tgBusy = false, dcBusy = false;
  let tgError = '', dcError = '';
  let tgNotice = '', dcNotice = '';

  // Loads both sections each time the editor opens (the only dependency named
  // is `open`, so the statement runs exactly when it flips).
  $: if (open) loadAll();

  async function loadAll() {
    loading = true;
    loadError = '';
    try {
      const r = await sendCommand('get_telegram_discord_config', {});
      if (r?.status === 'success') {
        tgDraft = {
          enabled: boolValue(r.telegram?.enabled, false),
          token: '',
          tokenSet: boolValue(r.telegram?.token_set, false),
        };
        dcDraft = {
          enabled: boolValue(r.discord?.enabled, false),
          token: '',
          tokenSet: boolValue(r.discord?.token_set, false),
          botTokenEnv: typeof r.discord?.bot_token_env === 'string' ? r.discord.bot_token_env : '',
          rate_limit_per_user_max: String(r.discord?.rate_limit_per_user_max ?? 10),
          rate_limit_per_user_window: String(r.discord?.rate_limit_per_user_window ?? 600),
          rate_limit_global_max: String(r.discord?.rate_limit_global_max ?? 60),
          rate_limit_global_window: String(r.discord?.rate_limit_global_window ?? 3600),
          response_delay_seconds: String(r.discord?.response_delay_seconds ?? 3),
          ttl_minutes: String(r.discord?.ttl_minutes ?? 30),
          max_messages: String(r.discord?.max_messages ?? 50),
        };
      } else if (r?.status === 'error') {
        loadError = r.message || 'Could not read the Telegram and Discord settings';
      }
      tgNotice = dcNotice = '';
      tgError = dcError = '';
    } catch (error) {
      loadError = `Error: ${error}`;
    } finally {
      loading = false;
    }
  }

  function close() {
    dispatch('close');
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && !tgBusy && !dcBusy) close();
  }

  // Each save sends only its section. A typed token goes along only when
  // non-empty — an empty field keeps the stored token — and the Remove
  // control is the one explicit clear.
  async function saveTelegram() {
    tgBusy = true;
    tgError = '';
    try {
      const telegram: Record<string, unknown> = { enabled: tgDraft.enabled };
      const token = tgDraft.token.trim();
      if (token) telegram.bot_token = token;
      const r = await sendCommand('save_telegram_discord_config', { config: { telegram } });
      if (r?.status === 'success') {
        tgDraft.token = '';
        if (token) tgDraft.tokenSet = true;
        tgNotice = 'Saved.';
      } else {
        tgError = r?.message || 'Could not save the Telegram settings';
      }
    } catch (error) {
      tgError = `Error: ${error}`;
    } finally {
      tgBusy = false;
    }
  }

  async function removeTelegramToken() {
    tgBusy = true;
    tgError = '';
    try {
      const r = await sendCommand('save_telegram_discord_config', {
        config: { telegram: { clear_bot_token: true } },
      });
      if (r?.status === 'success') {
        tgDraft.token = '';
        tgDraft.tokenSet = false;
        tgNotice = 'Stored token removed.';
      } else {
        tgError = r?.message || 'Could not remove the stored Telegram token';
      }
    } catch (error) {
      tgError = `Error: ${error}`;
    } finally {
      tgBusy = false;
    }
  }

  async function saveDiscord() {
    const perUserMax = parseWholeNumber(dcDraft.rate_limit_per_user_max, 1);
    if (perUserMax === null) {
      dcError = 'Per-user rate limit must be a whole number of messages, 1 or more.';
      dcNotice = '';
      return;
    }
    const perUserWindow = parseWholeNumber(dcDraft.rate_limit_per_user_window, 1);
    if (perUserWindow === null) {
      dcError = 'The per-user window must be a whole number of seconds, 1 or more.';
      dcNotice = '';
      return;
    }
    const globalMax = parseWholeNumber(dcDraft.rate_limit_global_max, 1);
    if (globalMax === null) {
      dcError = 'Global rate limit must be a whole number of messages, 1 or more.';
      dcNotice = '';
      return;
    }
    const globalWindow = parseWholeNumber(dcDraft.rate_limit_global_window, 1);
    if (globalWindow === null) {
      dcError = 'The global window must be a whole number of seconds, 1 or more.';
      dcNotice = '';
      return;
    }
    const delay = parseWholeNumber(dcDraft.response_delay_seconds, 0);
    if (delay === null) {
      dcError = 'Response delay must be a whole number of seconds, 0 or more (0 = answer at once).';
      dcNotice = '';
      return;
    }
    const ttl = parseWholeNumber(dcDraft.ttl_minutes, 1);
    if (ttl === null) {
      dcError = 'Conversation reset must be a whole number of minutes, 1 or more.';
      dcNotice = '';
      return;
    }
    const maxMessages = parseWholeNumber(dcDraft.max_messages, 1);
    if (maxMessages === null) {
      dcError = 'Messages kept per conversation must be a whole number, 1 or more.';
      dcNotice = '';
      return;
    }
    dcBusy = true;
    dcError = '';
    try {
      const discord: Record<string, unknown> = {
        enabled: dcDraft.enabled,
        rate_limit_per_user_max: perUserMax,
        rate_limit_per_user_window: perUserWindow,
        rate_limit_global_max: globalMax,
        rate_limit_global_window: globalWindow,
        response_delay_seconds: delay,
        ttl_minutes: ttl,
        max_messages: maxMessages,
      };
      const token = dcDraft.token.trim();
      if (token) discord.bot_token = token;
      const r = await sendCommand('save_telegram_discord_config', { config: { discord } });
      if (r?.status === 'success') {
        dcDraft.token = '';
        if (token) dcDraft.tokenSet = true;
        dcNotice = 'Saved.';
      } else {
        dcError = r?.message || 'Could not save the Discord settings';
      }
    } catch (error) {
      dcError = `Error: ${error}`;
    } finally {
      dcBusy = false;
    }
  }

  async function removeDiscordToken() {
    dcBusy = true;
    dcError = '';
    try {
      const r = await sendCommand('save_telegram_discord_config', {
        config: { discord: { clear_bot_token: true } },
      });
      if (r?.status === 'success') {
        dcDraft.token = '';
        dcDraft.tokenSet = false;
        dcNotice = 'Stored token removed.';
      } else {
        dcError = r?.message || 'Could not remove the stored Discord token';
      }
    } catch (error) {
      dcError = `Error: ${error}`;
    } finally {
      dcBusy = false;
    }
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="modal-overlay" on:keydown={handleKeydown} role="presentation">
    <div class="modal" role="dialog" aria-labelledby="telegram-discord-title" tabindex="-1">
      <div class="modal-header">
        <h2 id="telegram-discord-title">Telegram and Discord</h2>
        <button class="close-btn" on:click={close}>&times;</button>
      </div>

      <div class="modal-body">
        {#if loadError}
          <p class="section-error">{loadError}</p>
        {/if}

        <section class="settings-section">
          <h3>Telegram</h3>
          <p class="section-hint">
            Enable the Telegram bot and store its token here instead of editing
            config.ini by hand. Both are read once, when the backend starts and
            builds the bot (telegram_service.py): a save lands in config.ini and
            takes effect from the next backend start.
          </p>
          {#if tgError}
            <p class="section-error">{tgError}</p>
          {:else if tgNotice}
            <p class="section-saved">{tgNotice}</p>
          {/if}
          <div class="form-group">
            <label for="tg-enabled">
              <input id="tg-enabled" type="checkbox" bind:checked={tgDraft.enabled} disabled={tgBusy || loading} />
              Enable the Telegram bot
            </label>
            <span class="field-hint wide">
              Applies from the next backend start. With no token stored the bot
              cannot start at all: it logs that no bot_token is configured and
              stays off, however this switch is set.
            </span>
          </div>
          <div class="form-row">
            <label for="tg-token">Bot token</label>
            <div class="token-row">
              <input id="tg-token" type="password" autocomplete="off" bind:value={tgDraft.token}
                disabled={tgBusy || loading} placeholder={tokenPlaceholder(tgDraft.tokenSet)} />
              <button class="btn-remove" on:click={removeTelegramToken}
                disabled={tgBusy || loading || !tgDraft.tokenSet} title="Clear the stored token">
                Remove
              </button>
            </div>
            <span class="field-hint">
              A stored token is never sent back to this form: the field opens
              empty, leaving it empty keeps the stored token, and only Remove
              clears it. The token comes from @BotFather. Applies from the next
              backend start.
            </span>
          </div>
          <button class="btn-save" on:click={saveTelegram} disabled={tgBusy || loading}>
            {tgBusy ? 'Saving...' : 'Save Telegram settings'}
          </button>
        </section>

        <section class="settings-section">
          <h3>Discord</h3>
          <p class="section-hint">
            The switch and the token are read once, when the backend starts the
            Discord bot (discord_service.py): a save lands in config.ini and takes
            effect from the next backend start. The limits below are the live
            keys: discord_coordinator.py reads each one as a Discord message
            arrives, so a save applies to the next message — no restart.
          </p>
          {#if dcError}
            <p class="section-error">{dcError}</p>
          {:else if dcNotice}
            <p class="section-saved">{dcNotice}</p>
          {/if}
          <div class="form-group">
            <label for="dc-enabled">
              <input id="dc-enabled" type="checkbox" bind:checked={dcDraft.enabled} disabled={dcBusy || loading} />
              Enable the Discord bot
            </label>
            <span class="field-hint wide">
              Applies from the next backend start. When no token can be found the
              bot skips starting and logs a warning.
            </span>
          </div>
          <div class="form-row">
            <label for="dc-token">Bot token</label>
            <div class="token-row">
              <input id="dc-token" type="password" autocomplete="off" bind:value={dcDraft.token}
                disabled={dcBusy || loading} placeholder={tokenPlaceholder(dcDraft.tokenSet)} />
              <button class="btn-remove" on:click={removeDiscordToken}
                disabled={dcBusy || loading || !dcDraft.tokenSet} title="Clear the stored token">
                Remove
              </button>
            </div>
            {#if dcDraft.botTokenEnv}
              <span class="field-hint">
                discord.bot_token_env names the environment variable
                {dcDraft.botTokenEnv}: when that variable is set, the bot reads
                its token from there and the token stored here is only the
                fallback.
              </span>
            {:else}
              <span class="field-hint">
                A stored token is never sent back to this form: the field opens
                empty, leaving it empty keeps the stored token, and only Remove
                clears it. Applies from the next backend start.
              </span>
            {/if}
          </div>
          <div class="form-row">
            <label for="dc-per-user-max">Per-user rate limit</label>
            <input id="dc-per-user-max" type="number" min="1" step="1"
              bind:value={dcDraft.rate_limit_per_user_max} disabled={dcBusy || loading} />
            <span class="field-hint">
              Messages one user may send within the per-user window (default 10).
              Read per message: applies to the next message.
            </span>
          </div>
          <div class="form-row">
            <label for="dc-per-user-window">Per-user window (seconds)</label>
            <input id="dc-per-user-window" type="number" min="1" step="1"
              bind:value={dcDraft.rate_limit_per_user_window} disabled={dcBusy || loading} />
            <span class="field-hint">
              How long the per-user count stays (default 600).
            </span>
          </div>
          <div class="form-row">
            <label for="dc-global-max">Global rate limit</label>
            <input id="dc-global-max" type="number" min="1" step="1"
              bind:value={dcDraft.rate_limit_global_max} disabled={dcBusy || loading} />
            <span class="field-hint">
              Messages all users together may send within the global window
              (default 60).
            </span>
          </div>
          <div class="form-row">
            <label for="dc-global-window">Global window (seconds)</label>
            <input id="dc-global-window" type="number" min="1" step="1"
              bind:value={dcDraft.rate_limit_global_window} disabled={dcBusy || loading} />
            <span class="field-hint">
              How long the global count stays (default 3600).
            </span>
          </div>
          <div class="form-row">
            <label for="dc-delay">Response delay (seconds)</label>
            <input id="dc-delay" type="number" min="0" step="1"
              bind:value={dcDraft.response_delay_seconds} disabled={dcBusy || loading} />
            <span class="field-hint">
              Pause before the bot answers, so it does not look instant; 0 =
              answer at once (default 3).
            </span>
          </div>
          <div class="form-row">
            <label for="dc-ttl">Conversation reset (minutes)</label>
            <input id="dc-ttl" type="number" min="1" step="1"
              bind:value={dcDraft.ttl_minutes} disabled={dcBusy || loading} />
            <span class="field-hint">
              A user's conversation resets after this many idle minutes
              (default 30).
            </span>
          </div>
          <div class="form-row">
            <label for="dc-max-messages">Messages kept per conversation</label>
            <input id="dc-max-messages" type="number" min="1" step="1"
              bind:value={dcDraft.max_messages} disabled={dcBusy || loading} />
            <span class="field-hint">
              How many messages a conversation keeps (default 50).
            </span>
          </div>
          <button class="btn-save" on:click={saveDiscord} disabled={dcBusy || loading}>
            {dcBusy ? 'Saving...' : 'Save Discord settings'}
          </button>
        </section>
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .modal {
    background: white;
    border-radius: 8px;
    width: 95%;
    max-width: 640px;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.5rem;
    border-bottom: 1px solid #e0e0e0;
  }

  .modal-header h2 {
    margin: 0;
    font-size: 1.3rem;
    color: #333;
  }

  .close-btn {
    background: none;
    border: none;
    font-size: 2rem;
    cursor: pointer;
    color: #999;
    line-height: 1;
  }

  .close-btn:hover {
    color: #333;
  }

  .modal-body {
    padding: 1rem 1.5rem;
    overflow-y: auto;
  }

  .settings-section {
    padding: 0.75rem 0;
    border-bottom: 1px solid #e0e0e0;
  }

  .settings-section:last-child {
    border-bottom: none;
  }

  .settings-section h3 {
    margin: 0 0 0.25rem 0;
    font-size: 1.05rem;
    color: #333;
  }

  .section-hint {
    margin: 0 0 0.5rem 0;
    color: #666;
    font-size: 0.85rem;
  }

  .field-hint {
    display: block;
    color: #888;
    font-size: 0.8rem;
  }

  .field-hint.wide {
    margin-bottom: 0.5rem;
  }

  .section-error {
    padding: 0.5rem;
    background: #f8d7da;
    color: #721c24;
    border: 1px solid #f5c6cb;
    border-radius: 4px;
    font-size: 0.85rem;
    margin: 0 0 0.5rem 0;
  }

  .section-saved {
    padding: 0.5rem;
    background: #d4edda;
    color: #155724;
    border: 1px solid #c3e6cb;
    border-radius: 4px;
    font-size: 0.85rem;
    margin: 0 0 0.5rem 0;
  }

  .form-group {
    margin-bottom: 0.75rem;
  }

  .form-group label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.4rem;
    font-size: 0.9rem;
    color: #444;
    cursor: pointer;
  }

  .form-row {
    display: grid;
    grid-template-columns: 170px 1fr;
    align-items: center;
    column-gap: 0.75rem;
    row-gap: 0.15rem;
    margin-bottom: 0.75rem;
  }

  .form-row label {
    font-size: 0.9rem;
    font-weight: 500;
    color: #555;
  }

  .form-row input,
  .token-row input {
    padding: 0.45rem;
    border: 1px solid #ddd;
    border-radius: 4px;
    font-size: 0.95rem;
  }

  .form-row input:focus,
  .token-row input:focus {
    outline: none;
    border-color: #1976d2;
    box-shadow: 0 0 0 3px rgba(25, 118, 210, 0.1);
  }

  .form-row .field-hint {
    grid-column: 2;
    margin-bottom: 0.25rem;
  }

  .token-row {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .token-row input {
    flex: 1;
  }

  .btn-remove {
    padding: 0.45rem 0.9rem;
    background: #fff;
    color: #a94442;
    border: 1px solid #a94442;
    border-radius: 4px;
    font-size: 0.9rem;
    cursor: pointer;
  }

  .btn-remove:hover:not(:disabled) {
    background: #f8d7da;
  }

  .btn-remove:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-save {
    padding: 0.5rem 1rem;
    background: #1976d2;
    color: white;
    border: none;
    border-radius: 4px;
    font-size: 0.9rem;
    cursor: pointer;
    transition: background 0.2s;
  }

  .btn-save:hover:not(:disabled) {
    background: #1258a0;
  }

  .btn-save:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  @media (max-width: 560px) {
    .form-row {
      grid-template-columns: 1fr;
    }

    .form-row input,
    .form-row .field-hint {
      grid-column: 1;
    }
  }
</style>
