import { describe, it, expect } from 'vitest';
import core from './coreService.ts?raw';
import page from '../routes/+page.svelte?raw';

// Source guards: there is no component render here.
describe('a notice_toast (a local engine swap) is a toast, not an alert', () => {
  const handler = core.match(/message\.event === "notice_toast"\) \{([\s\S]*?)\n                \}/);

  it('coreService hands it to the noticeToast store and neither alerts nor logs an error', () => {
    expect(handler).not.toBeNull();
    expect(handler![1]).toContain('noticeToast.set(message.payload)');
    expect(handler![1]).not.toContain('alert(');
    expect(handler![1]).not.toContain('console.error');
  });

  it('error_toast keeps its blocking alert', () => {
    const m = core.match(/message\.event === "error_toast"\) \{([\s\S]*?)\n                \}/);
    expect(m![1]).toContain('alert(');
  });

  it('the page shows it for the duration the backend sent', () => {
    expect(page).toContain('const n = $noticeToast;');
    expect(page).toContain('agentToastDuration = ms;');
    expect(page).toContain('duration={agentToastDuration ??');
  });
});
