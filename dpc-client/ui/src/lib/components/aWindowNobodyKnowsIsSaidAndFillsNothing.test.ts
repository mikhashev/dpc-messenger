import { describe, it, expect } from 'vitest';
import controls from './SessionControls.svelte?raw';
import chatPanel from '../panels/ChatPanel.svelte?raw';
import historySync from '../panels/ChatHistorySyncPanel.svelte?raw';
import router from '../panels/MessageRouterPanel.svelte?raw';

// The backend sends token_limit 0 for a window nobody knows. These screens hold a default
// of 16,384 for a chat with no answer yet, and a percentage over that default is invented.

describe('the backend saying 0 reaches the header', () => {
  it('on a history answer, both of the times one is asked for', () => {
    const said = historySync.match(/windowUnknown: result\.token_limit === 0/g) ?? [];
    expect(said).toHaveLength(2);
    expect(historySync).not.toContain('result.token_limit > 0');
  });

  it('on the answer to a query', () => {
    expect(router).toContain('windowUnknown: message.payload.token_limit === 0');
    expect(router).toContain('message.payload.token_limit !== undefined');
  });
});

describe('the detailed counter of a chat with a measured prompt', () => {
  it('prints no limit and no percentage it would have to divide by the default for', () => {
    expect(controls).toContain('{#if !windowUnknown} / {dialogAvailable.toLocaleString()}{/if}');
    expect(controls).toContain('{#if !windowUnknown}({Math.round(dialogPercentWithInput * 100)}%){/if}');
    expect(controls).toContain('{#if !windowUnknown}({Math.round(totalContextPercentWithInput * 100)}%){/if}');
  });

  it('colours nothing as a warning', () => {
    expect(controls).toContain('class:warning={!windowUnknown && dialogPercentWithInput >= 0.8}');
    expect(controls).toContain('class:warning={!windowUnknown && totalContextPercentWithInput >= 0.8}');
  });
});

describe('the input box of a chat whose window is unknown', () => {
  it('is not closed as a full window', () => {
    expect(chatPanel).toContain(
      'isActuallyAIChat && !currentTokenUsage.windowUnknown && estimatedUsage.percentage >= 1.0',
    );
  });

  it('shows no banner saying the window is nearly full', () => {
    expect(chatPanel).toContain("!$aiChats.has(activeChatId) || currentTokenUsage.windowUnknown");
  });
});
