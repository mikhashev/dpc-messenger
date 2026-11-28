// src/lib/services/rejectedRecords.ts
// The rejected-record store: the chat records this node refused because their
// signature is wrong (DPC #111). The backend keeps them apart from the history
// and pushes each one as a `group_record_rejected` event; `get_rejected_records`
// answers with all the rows kept for one chat. A rejected record is not a
// message: it never enters chatHistories, processedMessageIds or an unread
// count, and nothing about it is ever sent back to the backend.
//
// The Map updates are pure and exported on their own so they can be unit-tested
// in node without the store's wiring (which imports coreService for sendCommand).

import { writable, get } from 'svelte/store';
import type { RejectedRecord } from '$lib/types';
import { sendCommand } from '$lib/coreService';

/** conversation id -> the rejected rows kept for that chat. */
export const rejectedRecords = writable<Map<string, RejectedRecord[]>>(new Map());

/** Insert a row under its conversation, or replace the one already held under
 *  the same key (a repeat or a refresh of the same refused record) in place.
 *  Pure: always a new Map, the given one is not mutated. */
export function upsertRejectedRecordInMap(
  map: Map<string, RejectedRecord[]>,
  record: RejectedRecord,
): Map<string, RejectedRecord[]> {
  const rows = map.get(record.conversation_id) ?? [];
  const at = rows.findIndex((r) => r.key === record.key);
  const next = at >= 0
    ? rows.map((r, i) => (i === at ? record : r))
    : [...rows, record];
  const out = new Map(map);
  out.set(record.conversation_id, next);
  return out;
}

/** Swap the whole list held for one conversation; other conversations are
 *  untouched. Pure: always a new Map. */
export function replaceRejectedRecordsInMap(
  map: Map<string, RejectedRecord[]>,
  conversationId: string,
  records: RejectedRecord[],
): Map<string, RejectedRecord[]> {
  const out = new Map(map);
  out.set(conversationId, records);
  return out;
}

/** Keys upserted by a live event while a load was in flight for that chat. A
 *  conversation with no load in flight has no entry: `loadRejectedRecords`
 *  installs the set before sending and removes it in its `finally`. */
const keysArrivedDuringLoad = new Map<string, Set<string>>();

/** One `group_record_rejected` payload joins its chat's rows, or refreshes the
 *  row already held under its key. A load in flight for that chat may answer
 *  without this row (its snapshot was taken earlier), so the key is remembered
 *  for the merge that load will run. */
export function upsertRejectedRecord(record: RejectedRecord): void {
  keysArrivedDuringLoad.get(record.conversation_id)?.add(record.key);
  rejectedRecords.update((map) => upsertRejectedRecordInMap(map, record));
}

export function replaceRejectedRecords(conversationId: string, records: RejectedRecord[]): void {
  rejectedRecords.update((map) => replaceRejectedRecordsInMap(map, conversationId, records));
}

/** Fold a `get_rejected_records` answer into the rows already held, without
 *  losing a live event that landed while the request was in flight: the answer
 *  is a snapshot taken before that event, so on its own it would erase the row
 *  from the chat until the chat was reopened.
 *
 *  Everything the answer holds is kept. A row of `current` whose key was
 *  upserted during the load survives: appended when the answer lacks it, or
 *  kept — in the answer's position — over the answer's row when its
 *  `repeat_count` is greater (the count only grows, so the greater count is
 *  the newer row; a tie keeps the answer's, the side this merge trusts). Rows
 *  of `current` that are neither in the answer nor remembered as arrived were
 *  gone from the backend before the snapshot: the answer is authoritative for
 *  them and they are dropped. Pure: the rows are returned as they are, never
 *  mutated. */
export function mergeRejectedAnswer(
  answer: RejectedRecord[],
  current: RejectedRecord[],
  arrivedDuringLoad: ReadonlySet<string>,
): RejectedRecord[] {
  const out = [...answer];
  const answerAt = new Map(out.map((r, i) => [r.key, i] as const));
  for (const row of current) {
    if (!arrivedDuringLoad.has(row.key)) continue;
    const at = answerAt.get(row.key);
    if (at === undefined) {
      answerAt.set(row.key, out.length);
      out.push(row);
    } else if (row.repeat_count > out[at].repeat_count) {
      out[at] = row;
    }
  }
  return out;
}

/** Ask the backend for the rows kept for one chat (the rows ride along with the
 *  history panel's newest page). The answer is a snapshot, so rows that arrived
 *  live while it was in flight are merged over it rather than erased by it.
 *  The store is left alone on any failure — rows already on screen stay there —
 *  with one warn, as the neighbouring loaders do. */
export async function loadRejectedRecords(conversationId: string): Promise<void> {
  const arrived = new Set<string>();
  keysArrivedDuringLoad.set(conversationId, arrived);
  try {
    const sent = sendCommand('get_rejected_records', { conversation_id: conversationId });
    if (sent === false) {
      console.warn(`[RejectedRecords] Not connected - keeping the rows already held for ${conversationId}`);
      return;
    }
    const result = await sent;
    if (result?.status === 'success' && result?.conversation_id === conversationId) {
      const answerRows: RejectedRecord[] = Array.isArray(result.records) ? result.records : [];
      const merged = mergeRejectedAnswer(
        answerRows, get(rejectedRecords).get(conversationId) ?? [], arrived);
      replaceRejectedRecords(conversationId, merged);
      return;
    }
    console.warn(`[RejectedRecords] ${conversationId}: ${result?.message ?? result?.status ?? 'no answer'}`);
  } catch (e) {
    console.warn(`[RejectedRecords] ${conversationId}:`, e);
  } finally {
    // Only this load's own set: a second load for the same chat may have
    // installed its own while this one was answering. A chat with no load in
    // flight is back to having no entry.
    if (keysArrivedDuringLoad.get(conversationId) === arrived) {
      keysArrivedDuringLoad.delete(conversationId);
    }
  }
}
