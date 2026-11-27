import { describe, it, expect } from 'vitest';
import editor from './TelegramDiscordSettingsEditor.svelte?raw';
import core from '../coreService.ts?raw';
import sidebar from './Sidebar.svelte?raw';
import page from '../../routes/+page.svelte?raw';

// No mount harness: the form is checked in the source, the way the other settings-form
// guards do (providersEditorIdleSwap.test.ts). The card is
// THE-TELEGRAM-AND-DISCORD-SETTINGS-CAN-ONLY-BE-TYPED-INTO-CONFIG-INI.

describe('the Telegram and Discord form covers the switches, the tokens and the live limits', () => {
  it('has the two sections, each with its own save button', () => {
    expect(editor).toContain('<h3>Telegram</h3>');
    expect(editor).toContain('<h3>Discord</h3>');
    const saves = editor.match(/class="btn-save" on:click=\{save(Telegram|Discord)\}/g) ?? [];
    expect(saves.length).toBe(2);
  });

  it('carries the two enable switches and none of the dead telegram keys', () => {
    for (const id of ['tg-enabled', 'dc-enabled']) {
      expect(editor).toContain(`id="${id}"`);
    }
    // The [telegram] history keys are dead (another card owns them); the
    // whitelist-channels/webhook/guild wiring keys are not this form's either.
    for (const absent of [
      'fetch_history_on_startup', 'history_fetch_limit', 'history_max_age_hours',
      'history_message_types', 'drop_pending_updates', 'allowed_chat_ids',
      'use_webhook', 'webhook_url', 'guild_id', 'allowed_channel_ids',
      'ark_channel_id', 'agent_id', 'mirror_group_id', 'bot_token_env"',
    ]) {
      expect(editor).not.toContain(`id="${absent}"`);
    }
  });

  it('carries the seven live discord limit keys as number fields', () => {
    for (const id of ['dc-per-user-max', 'dc-per-user-window', 'dc-global-max',
      'dc-global-window', 'dc-delay', 'dc-ttl', 'dc-max-messages']) {
      expect(editor).toContain(`id="${id}"`);
      expect(editor).toContain(`type="number"`);
    }
  });
});

describe('a stored token is never sent back to the form as text', () => {
  it('shows password fields that open empty, with dots and a keep rule when a token is stored', () => {
    for (const id of ['tg-token', 'dc-token']) {
      expect(editor).toContain(`id="${id}"`);
    }
    expect((editor.match(/type="password"/g) ?? []).length).toBe(2);
    // The placeholder comes from tokenPlaceholder(token_set) — dots plus the
    // keep rule, never a value.
    expect(editor).toContain('placeholder={tokenPlaceholder(tgDraft.tokenSet)}');
    expect(editor).toContain('placeholder={tokenPlaceholder(dcDraft.tokenSet)}');
    // The load path binds presence only: the token fields are set to the empty
    // string, and no reply field is ever bound into them (bot_token_env is the
    // environment variable *name*, a non-secret the hint shows).
    expect(editor).toMatch(/token: '',\s*\n\s*tokenSet:/);
    expect(editor).not.toContain('token = r.');
    expect(editor).not.toContain('token: r.');
  });

  it('has an explicit Remove control per section, and only that clears', () => {
    expect((editor.match(/clear_bot_token: true/g) ?? []).length).toBe(2);
    expect(editor).toContain('Stored token removed.');
    // A typed token rides along only when non-empty — an empty field keeps it.
    expect(editor).toContain('if (token) telegram.bot_token = token;');
    expect(editor).toContain('if (token) discord.bot_token = token;');
  });

  it('says the never-sent-back rule beside both token fields', () => {
    expect((editor.match(/A stored token is never sent back to this form/g) ?? []).length)
      .toBeGreaterThanOrEqual(2);
  });
});

describe('each save sends only its section payload to the one command', () => {
  it('the telegram payload holds the switch and, only when typed, the token', () => {
    expect(editor).toContain('config: { telegram }');
    expect(editor).toContain('config: { telegram: { clear_bot_token: true } }');
    expect(editor).toContain('config: { discord }');
    expect(editor).toContain('config: { discord: { clear_bot_token: true } }');
  });

  it('the discord payload holds the switch and the seven parsed limits', () => {
    for (const key of ['rate_limit_per_user_max', 'rate_limit_per_user_window',
      'rate_limit_global_max', 'rate_limit_global_window', 'response_delay_seconds',
      'ttl_minutes', 'max_messages']) {
      expect(editor).toContain(`${key}:`);
    }
    // Every limit is parsed client-side first, with its own words.
    expect((editor.match(/parseWholeNumber\(dcDraft\./g) ?? []).length).toBe(7);
  });

  it('a refused or failed save shows the backend reason beside its section', () => {
    expect((editor.match(/r\?\.message \|\| 'Could not (save|remove)/g) ?? []).length).toBe(4);
  });
});

describe('the form is honest about when each key takes effect', () => {
  it('the switches and tokens name the next backend start, twice per section', () => {
    const notes = editor.match(/next backend start/g) ?? [];
    expect(notes.length).toBeGreaterThanOrEqual(4);
  });

  it('the limits are named as the live keys, read per message', () => {
    expect(editor).toContain('applies to the next message');
    expect(editor).toContain('no restart');
  });

  it('says what an empty token means for each bot', () => {
    expect(editor).toContain('no bot_token is configured');
    expect(editor).toContain('skips starting');
  });

  it('says when the environment variable wins over the stored discord token', () => {
    expect(editor).toContain('bot_token_env names the environment variable');
  });
});

describe('the form loads its keys and opens from the sidebar', () => {
  it('loads the get when it opens', () => {
    expect(editor).toContain('$: if (open) loadAll();');
    expect(editor).toContain("'get_telegram_discord_config'");
  });

  it('coreService carries the two new command helpers', () => {
    for (const fn of ['getTelegramDiscordConfig', 'saveTelegramDiscordConfig']) {
      expect(core).toContain(`export async function ${fn}(`);
    }
  });

  it('the sidebar has the button and the page mounts the editor', () => {
    expect(sidebar).toContain('onOpenTelegramDiscord');
    expect(sidebar).toContain('Telegram and Discord');
    expect(page).toContain('import TelegramDiscordSettingsEditor from "$lib/components/TelegramDiscordSettingsEditor.svelte"');
    expect(page).toContain('bind:open={showTelegramDiscordEditor}');
  });
});
