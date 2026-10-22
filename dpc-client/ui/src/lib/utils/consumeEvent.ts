// src/lib/utils/consumeEvent.ts
// Take-and-clear for "last event" stores.
//
// A store that holds the latest backend event (e.g. groupHistorySynced) stays
// set until something resets it. An effect that reads it re-runs whenever any
// other dependency changes, so a handled event must be copied out and the
// store cleared in the same step, or the stale payload is handled again.

import type { Writable } from 'svelte/store';
import { get } from 'svelte/store';

/** Return a shallow copy of the store's event and reset the store to null; null if none pending. */
export function consumeEvent<T extends object>(store: Writable<T | null>): T | null {
  const current = get(store);
  if (!current) return null;
  const copy = { ...current };
  store.set(null);
  return copy;
}
