import { describe, it, expect } from 'vitest';
import { writable } from 'svelte/store';
import { consumeEvent } from './consumeEvent';

describe('consumeEvent', () => {
  it('returns the pending event once, then null (no replay on a later re-run)', () => {
    const store = writable<{ group_id: string; message_count: number } | null>(null);
    store.set({ group_id: 'group-1', message_count: 65 });
    expect(consumeEvent(store)).toEqual({ group_id: 'group-1', message_count: 65 });
    expect(consumeEvent(store)).toBeNull();
  });

  it('resets the store itself', () => {
    const store = writable<{ group_id: string } | null>({ group_id: 'g' });
    consumeEvent(store);
    let seen: unknown = 'unset';
    store.subscribe(v => (seen = v))();
    expect(seen).toBeNull();
  });

  it('returns null for an empty store', () => {
    expect(consumeEvent(writable<{ a: number } | null>(null))).toBeNull();
  });
});
