/**
 * rejectedRecords.ts: the pure Map updates under the rejected-record store
 * (DPC #111), and the merge that keeps a live event from being erased by the
 * snapshot answer of the load that was in flight when it arrived. An event
 * upserts by the row's stable key — a repeat refreshes the row it already
 * placed rather than adding a second one — and the answer of
 * `get_rejected_records` is authoritative except for the keys that arrived
 * while it was being fetched.
 *
 * The store's wiring imports coreService for sendCommand; the command sender
 * is mocked (the same shape music.test.ts gives its service), which also lets
 * the load itself be driven here: its answer is released by hand, around a
 * live upsert, exactly as the lost update happens in the app.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import {
  upsertRejectedRecordInMap, replaceRejectedRecordsInMap, mergeRejectedAnswer,
  upsertRejectedRecord, loadRejectedRecords, rejectedRecords,
} from './rejectedRecords';
import { sendCommand } from '$lib/coreService';
import type { RejectedRecord } from '$lib/types';

vi.mock('$lib/coreService', () => ({ sendCommand: vi.fn() }));
const sendCommandMock = vi.mocked(sendCommand);

beforeEach(() => {
  rejectedRecords.set(new Map());
  sendCommandMock.mockReset();
});

function row(key: string, text = 'refused', conversation_id = 'group-a'): RejectedRecord {
  return {
    key,
    conversation_id,
    record_id: null,
    reason: 'invalid_signature',
    transport_node_id: null,
    claimed_sender_node_id: null,
    claimed_sender_name: null,
    claimed_sender_type: null,
    claimed_agent_owner: null,
    signer_node_id: null,
    claimed_content_hash: null,
    computed_content_hash: null,
    signature: null,
    preimage_version: null,
    record_timestamp: null,
    first_seen: '2026-11-28T10:00:00Z',
    last_seen: '2026-11-28T10:00:00Z',
    repeat_count: 1,
    live_count: 1,
    sync_count: 0,
    repair_count: 0,
    text,
    text_truncated: false,
  };
}

describe('upsertRejectedRecordInMap', () => {
  it('inserts the first row held for a conversation', () => {
    const out = upsertRejectedRecordInMap(new Map(), row('k1'));
    expect(out.get('group-a')?.map((r) => r.key)).toEqual(['k1']);
  });

  it('appends a new key after the rows already held', () => {
    const out = upsertRejectedRecordInMap(
      upsertRejectedRecordInMap(new Map(), row('k1')),
      row('k2'));
    expect(out.get('group-a')?.map((r) => r.key)).toEqual(['k1', 'k2']);
  });

  it('replaces by key and keeps the row\'s position', () => {
    const before = upsertRejectedRecordInMap(
      upsertRejectedRecordInMap(new Map(), row('k1')),
      row('k2'));
    const out = upsertRejectedRecordInMap(before, row('k1', 'refused again, 3 times'));
    const rows = out.get('group-a') ?? [];
    expect(rows.map((r) => r.key)).toEqual(['k1', 'k2']);  // the position is kept
    expect(rows[0].text).toBe('refused again, 3 times');   // the content is replaced
  });

  it('leaves another conversation\'s rows untouched', () => {
    const before = upsertRejectedRecordInMap(new Map(), row('other-1', 'x', 'group-b'));
    const out = upsertRejectedRecordInMap(before, row('k1'));
    expect(out.get('group-b')?.map((r) => r.key)).toEqual(['other-1']);
    expect(out.get('group-a')?.map((r) => r.key)).toEqual(['k1']);
  });

  it('does not mutate the map it was given', () => {
    const before = upsertRejectedRecordInMap(new Map(), row('k1'));
    const snapshot = new Map(before);
    upsertRejectedRecordInMap(before, row('k2'));
    expect(before).toEqual(snapshot);
  });

  it('upserts a conflict row like any other, by its key', () => {
    // A conflict can arrive by repair and later live: one key, one row, the
    // counts moved onto it — the reason changes nothing about the identity.
    const out = upsertRejectedRecordInMap(
      new Map(),
      row('k-conflict', 'transfer denied'));
    const again = { ...row('k-conflict', 'transfer denied'),
                    reason: 'conflict', repair_count: 1, repeat_count: 2 } as RejectedRecord;
    const merged = upsertRejectedRecordInMap(out, again);
    expect(merged.get('group-a')?.map((r) => r.key)).toEqual(['k-conflict']);
    expect(merged.get('group-a')?.[0].reason).toBe('conflict');
    expect(merged.get('group-a')?.[0].repair_count).toBe(1);
  });
});

describe('replaceRejectedRecordsInMap', () => {
  it('swaps the whole list held for the conversation', () => {
    const before = upsertRejectedRecordInMap(
      upsertRejectedRecordInMap(new Map(), row('k1')),
      row('k2'));
    const out = replaceRejectedRecordsInMap(before, 'group-a', [row('only')]);
    expect(out.get('group-a')?.map((r) => r.key)).toEqual(['only']);
  });

  it('empties the list when the answer holds no rows', () => {
    const before = upsertRejectedRecordInMap(new Map(), row('k1'));
    const out = replaceRejectedRecordsInMap(before, 'group-a', []);
    expect(out.get('group-a')).toEqual([]);
  });

  it('leaves other conversations untouched', () => {
    const before = upsertRejectedRecordInMap(new Map(), row('b1', 'x', 'group-b'));
    const out = replaceRejectedRecordsInMap(before, 'group-a', [row('a1')]);
    expect(out.get('group-b')?.map((r) => r.key)).toEqual(['b1']);
    expect(out.get('group-a')?.map((r) => r.key)).toEqual(['a1']);
  });
});

describe('mergeRejectedAnswer', () => {
  it('keeps a row that arrived live during the load, over an answer that lacks it', () => {
    const answer = [row('a1')];
    const current = [row('live-1')];
    const out = mergeRejectedAnswer(answer, current, new Set(['live-1']));
    expect(out.map((r) => r.key)).toEqual(['a1', 'live-1']);
    // The rows are returned as they are; neither side was mutated.
    expect(out[1]).toBe(current[0]);
    expect(answer).toEqual([row('a1')]);
    expect(current).toEqual([row('live-1')]);
  });

  it('keeps whichever side has the greater repeat_count, and a tie keeps the answer\'s', () => {
    // The live row is the newer one: it survives in the answer's position.
    const answerFew = { ...row('k', 'from the answer'), repeat_count: 2 };
    const liveMany = { ...row('k', 'from the live event'), repeat_count: 5 };
    expect(mergeRejectedAnswer([answerFew], [liveMany], new Set(['k']))).toEqual([liveMany]);

    // The answer's row is the newer one: it stays.
    const answerMany = { ...row('k', 'from the answer'), repeat_count: 7 };
    const liveFew = { ...row('k', 'from the live event'), repeat_count: 3 };
    expect(mergeRejectedAnswer([answerMany], [liveFew], new Set(['k'])))
      .toEqual([{ ...answerMany }]);

    // A tie cannot tell which is newer: the answer's row is the one kept.
    const answerTie = { ...row('k', 'from the answer'), repeat_count: 4 };
    const liveTie = { ...row('k', 'from the live event'), repeat_count: 4 };
    const tied = mergeRejectedAnswer([answerTie], [liveTie], new Set(['k']));
    expect(tied).toHaveLength(1);
    expect(tied[0]).toBe(answerTie);
  });

  it('drops a stale row the answer does not hold and no live event refreshed', () => {
    const out = mergeRejectedAnswer([row('a1')], [row('stale')], new Set());
    expect(out.map((r) => r.key)).toEqual(['a1']);
  });

  it('is unmoved by a key that arrived during another conversation\'s load', () => {
    // The other chat's in-flight set names its own row; this chat's stale row
    // does not survive for sharing a store with it.
    const out = mergeRejectedAnswer([row('a1')], [row('stale')], new Set(['other-chat-row']));
    expect(out.map((r) => r.key)).toEqual(['a1']);
  });
});

describe('a live event during a load is not erased by its snapshot', () => {
  /** A load whose answer the test releases by hand, as the backend would. */
  function deferredAnswer(): (answer: unknown) => void {
    let resolveAnswer!: (value: unknown) => void;
    sendCommandMock.mockImplementation(() => new Promise((res) => { resolveAnswer = res; }));
    return (answer: unknown) => resolveAnswer(answer);
  }

  it('keeps a row upserted between sendCommand and its answer', async () => {
    const answer = deferredAnswer();
    const load = loadRejectedRecords('group-live');
    upsertRejectedRecord(row('live-1', 'arrived live', 'group-live'));
    answer({
      status: 'success', conversation_id: 'group-live',
      records: [row('old-1', 'from the snapshot', 'group-live')],
    });
    await load;
    expect(get(rejectedRecords).get('group-live')?.map((r) => r.key)).toEqual(['old-1', 'live-1']);
  });

  it('stops remembering arrivals once the load has ended', async () => {
    const firstAnswer = deferredAnswer();
    const first = loadRejectedRecords('group-tidy');
    upsertRejectedRecord(row('during', 'while the first load flew', 'group-tidy'));
    firstAnswer({ status: 'success', conversation_id: 'group-tidy', records: [] });
    await first;
    expect(get(rejectedRecords).get('group-tidy')?.map((r) => r.key)).toEqual(['during']);

    // Between the two loads nothing is in flight, so this upsert is not
    // remembered as an arrival: the next answer is authoritative for it.
    upsertRejectedRecord(row('between', 'after the first load', 'group-tidy'));
    const secondAnswer = deferredAnswer();
    const second = loadRejectedRecords('group-tidy');
    secondAnswer({ status: 'success', conversation_id: 'group-tidy', records: [] });
    await second;
    expect(get(rejectedRecords).get('group-tidy')).toEqual([]);
  });
});
