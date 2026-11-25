// The GPU card's queue as the UI holds it (ADR-052; Mike, 2026-11-25): seeded by the Agent map's
// `get_card_lease_status` read, replaced by every `card_lease_updated` event the backend pushes when
// who holds the card or who waits for it changes. The newest snapshot by its own clock wins.
// `cardLeaseFreshAt` is when the last snapshot arrived (Date.now()), read or pushed: the Agent map
// says the block is stale when none came for a while (follow-up 14, Zcode #176).

import { writable } from 'svelte/store';
import { isCardLeaseSnapshot, newerSnapshot, type CardLeaseSnapshot } from '../utils/cardLease';

export const cardLease = writable<CardLeaseSnapshot | null>(null);
export const cardLeaseFreshAt = writable<number | null>(null);

export function onCardLeaseUpdated(payload: unknown, nowMs: number = Date.now()): void {
    cardLease.update((current) => newerSnapshot(current, payload));
    if (isCardLeaseSnapshot(payload)) cardLeaseFreshAt.set(nowMs);
}

/** After a reconnect the events in between are lost: the next read seeds again. */
export function forgetCardLease(): void {
    cardLease.set(null);
    cardLeaseFreshAt.set(null);
}
