/**
 * The rejected-record row in the group chat (DPC #111): one snippet, rendered
 * in front of the message its time precedes and once after the loop, so a chat
 * with no messages still shows its refused records. The refused text is
 * untrusted — it may carry instructions for agents — so it may only ever
 * appear as `{row.text}` inside a plain <pre>: never through the markdown
 * renderer, never as HTML. And the row is not a message: no unread count in
 * the event branch, no control but the toggle that unfolds the text.
 *
 * Vitest runs in `environment: 'node'` here and the repo carries no DOM
 * harness, so this reads the sources, as the neighbouring guards do.
 */

import { describe, it, expect } from 'vitest';
import chat from './ChatMessageList.svelte?raw';
import core from '../coreService.ts?raw';
import historyPanel from '../panels/ChatHistorySyncPanel.svelte?raw';

/** The source of the row's snippet, from its declaration to its end. */
function rejectedRowSource(): string {
  const start = chat.indexOf('{#snippet rejectedRow(');
  expect(start, 'the rejectedRow snippet exists').toBeGreaterThan(-1);
  const end = chat.indexOf('{/snippet}', start);
  return chat.slice(start, end);
}

describe('the rejected row in ChatMessageList', () => {
  it('defines exactly one snippet for the row and renders it inside the loop and after it', () => {
    expect([...chat.matchAll(/\{#snippet rejectedRow\(/g)]).toHaveLength(1);
    const renders = [...chat.matchAll(/\{@render rejectedRow\(row\)\}/g)];
    expect(renders).toHaveLength(2);
    // In front of the message at index i: inside the messages loop.
    const loop = chat.indexOf('{#each displayMessages as msg, i (msg.id)}');
    const loopEnd = chat.indexOf('{/each}', loop);
    expect(renders[0].index).toBeGreaterThan(loop);
    expect(renders[0].index).toBeLessThan(loopEnd);
    // The last slot renders even when the chat holds no message: outside the
    // "has messages" condition, past the empty-chat branch.
    const emptyChat = chat.indexOf('<div class="empty-chat">');
    expect(renders[1].index).toBeGreaterThan(emptyChat);
  });

  it('places its rows from its own chat against the displayed messages', () => {
    expect(chat).toMatch(/\$rejectedRecords\.get\(conversationId\)/);
    expect(chat).toMatch(/rejectedRowSlots\(displayMessages,/);
  });

  it('shows the refused text only as text, inside the pre', () => {
    expect([...chat.matchAll(/\{row\.text\}/g)]).toHaveLength(1);
    expect(chat).toMatch(/<pre class="rejected-text">\{row\.text\}<\/pre>/);
  });

  it('renders the row without the markdown renderer or any HTML injection', () => {
    const row = rejectedRowSource();
    // The whole snippet — the row's opening <div class="rejected-row"> through
    // the toggle's end and beyond, to the snippet's own end — stays plain.
    expect(row).not.toContain('{@html');
    expect(row).not.toContain('MarkdownMessage');
    expect(row).not.toContain('escapeHtml(');
    expect(row).not.toContain('highlightMentions(');
  });

  it('folds the text away until the toggle opens it', () => {
    const row = rejectedRowSource();
    const gate = row.indexOf('{#if expanded}');
    const pre = row.indexOf('<pre class="rejected-text">');
    expect(gate).toBeGreaterThan(-1);
    expect(pre).toBeGreaterThan(gate);
    expect(row).toContain('aria-expanded={expanded}');
    expect(row).toMatch(/\{expanded \? '▼' : '▶'\} text/);
  });

  it('chooses the plural of "time" from the count, never a bare " times"', () => {
    const row = rejectedRowSource();
    expect(row).not.toContain(' times');
    expect(row).toMatch(/\{row\.repeat_count\} \{timesWord\(row\.repeat_count\)\}/);
    expect(chat).toMatch(/function timesWord\(count: number\): string \{\s*return count === 1 \? 'time' : 'times';\s*\}/);
  });

  it('prints the header time only when the row\'s own time parsed', () => {
    const row = rejectedRowSource();
    const timestamp = row.match(/<span class="timestamp">([\s\S]*?)<\/span>/)?.[1] ?? '';
    expect(timestamp).toMatch(/\{#if !Number\.isNaN\(ms\)\}/);
    expect(timestamp).toContain('new Date(ms).toLocaleTimeString()');
  });

  it('says conflict in the header only for a conflict, rejected otherwise', () => {
    // Nothing is wrong with a conflicting record on its own — what is wrong is
    // that another content is already held under its id — so the header names
    // the situation, not the verdict every other row carries.
    const row = rejectedRowSource();
    expect(row).toMatch(/\{row\.reason === 'conflict' \? 'conflict' : 'rejected'\}/);
  });

  it('counts repair arrivals in the same shape as the live and sync counts, only when there are any', () => {
    const row = rejectedRowSource();
    expect(row).toMatch(/\{#if row\.repair_count > 0\}· repair ×\{row\.repair_count\}\{\/if\}/);
    // It sits after the sync count, as the third way a row can arrive.
    const sync = row.indexOf('{#if row.sync_count > 0}');
    const repair = row.indexOf('{#if row.repair_count > 0}');
    expect(sync).toBeGreaterThan(-1);
    expect(repair).toBeGreaterThan(sync);
  });

  it('renders the toggle and the text only when the record carries text', () => {
    const row = rejectedRowSource();
    const gate = row.indexOf('{#if row.text}');
    const button = row.indexOf('<button');
    const buttonEnd = row.indexOf('</button>', button);
    const pre = row.indexOf('<pre class="rejected-text">');
    const gateClose = row.indexOf('{/if}', gate);
    expect(gate).toBeGreaterThan(-1);
    // The whole control — button and the unfolded text — sits inside the gate,
    // so a record with no text keeps only its facts line.
    expect(button).toBeGreaterThan(gate);
    expect(pre).toBeGreaterThan(gate);
    expect(buttonEnd).toBeLessThan(gateClose);
    expect(pre).toBeLessThan(gateClose);
  });

  it('wears the error bubble\'s reds', () => {
    const rule = chat.match(/\.rejected-row\s*\{([^}]*)\}/)?.[1] ?? '';
    expect(rule).toContain('background: #fff5f5');
    expect(rule).toContain('border: 1px solid #fc8181');
    expect(rule).toContain('border-left: 4px solid #e53e3e');
    const strong = chat.match(/\.rejected-row \.message-header strong\s*\{([^}]*)\}/)?.[1] ?? '';
    expect(strong).toContain('color: #c53030');
  });

  it('offers no action but the toggle: no reply, quote, copy or send back', () => {
    const row = rejectedRowSource();
    expect((row.match(/<button/g) ?? []).length).toBe(1);
    expect(row).toContain('class="rejected-toggle"');
    expect(row).not.toContain('sendCommand');
  });
});

describe('the group_record_rejected event in coreService', () => {
  function branch(): string {
    const at = core.indexOf('message.event === "group_record_rejected"');
    expect(at, 'the group_record_rejected branch exists').toBeGreaterThan(-1);
    const next = core.indexOf('else if (message.event ===', at);
    return core.slice(at, next);
  }

  it('upserts the payload into the rejected-records store', () => {
    expect(branch()).toContain('upsertRejectedRecord(message.payload)');
  });

  it('does not count a rejected record as unread — it is not a message', () => {
    expect(branch()).not.toContain('unreadMessageCounts');
  });
});

describe('the history panel loads the rejected rows with the newest page', () => {
  it('calls loadRejectedRecords once, for the opened group chat', () => {
    expect(historyPanel).toMatch(/reqChatId\.startsWith\('group-'\)\) loadRejectedRecords\(reqChatId\);/);
    // Once means the import aside there is no second call site — in
    // particular none in the load-older path, which speaks of chatId.
    expect([...historyPanel.matchAll(/loadRejectedRecords\(/g)]).toHaveLength(1);
  });

  it('asks after the page answered, empty or not, and not in its catch', () => {
    const call = historyPanel.indexOf('loadRejectedRecords(reqChatId)');
    const noMessagesBranch = historyPanel.indexOf('No messages: status=');
    const catchError = historyPanel.indexOf('Error loading history:');
    expect(call).toBeGreaterThan(noMessagesBranch);
    expect(call).toBeLessThan(catchError);
  });
});
