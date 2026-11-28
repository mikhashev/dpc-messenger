/**
 * rejectedRows.ts: the pure helpers of the rejected-record row (DPC #111). The
 * reason must always say something a person can read (a reason a later backend
 * adds shows itself rather than a blank line), the row must sit at the record's
 * own time — never the clock, which would drag an old row to the bottom on
 * every render — and the slot arithmetic must put each row in front of the
 * message its time precedes, with a row of the same time going before the
 * message rather than after it.
 */

import { describe, it, expect } from 'vitest';
import { REJECTED_REASON_TEXT, rejectedReasonText, rejectedRecordTime, rejectedRowSlots } from './rejectedRows';
import type { RejectedRecord } from '$lib/types';

function record(overrides: Partial<RejectedRecord> = {}): RejectedRecord {
  return {
    key: 'k1',
    conversation_id: 'group-x',
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
    text: 'refused',
    text_truncated: false,
    ...overrides,
  };
}

const iso = (ms: number): string => new Date(ms).toISOString();

describe('rejectedReasonText', () => {
  it.each(Object.entries(REJECTED_REASON_TEXT))('says the words for %s', (code, words) => {
    expect(rejectedReasonText(code as RejectedRecord['reason'])).toBe(words);
  });

  it('shows a code it does not know as itself, never an empty line', () => {
    expect(rejectedReasonText('made_up_code')).toBe('made_up_code');
  });

  it.each([undefined, null, ''])('says just the row\'s word for %s', (value) => {
    expect(rejectedReasonText(value as RejectedRecord['reason'] | string | null | undefined)).toBe('rejected');
  });
});

describe('rejectedRecordTime', () => {
  it('places the row by the record\'s own time when it parses', () => {
    const ms = rejectedRecordTime(
      record({ record_timestamp: '2026-11-28T09:00:00Z', first_seen: '2026-11-28T10:00:00Z' }));
    expect(ms).toBe(Date.parse('2026-11-28T09:00:00Z'));
  });

  it('falls back to first_seen when the record\'s own time is garbage', () => {
    const ms = rejectedRecordTime(
      record({ record_timestamp: 'not a time', first_seen: '2026-11-28T10:00:00Z' }));
    expect(ms).toBe(Date.parse('2026-11-28T10:00:00Z'));
  });

  it('is NaN when nothing parses, not the clock', () => {
    const ms = rejectedRecordTime(
      record({ record_timestamp: 'garbage', first_seen: 'also garbage', last_seen: '' }));
    expect(ms).toBeNaN();
  });
});

describe('rejectedRowSlots', () => {
  const messages = [{ timestamp: 1_000 }, { timestamp: 2_000 }, { timestamp: 3_000 }];

  it('puts a row before the first message, one between two, one after the last', () => {
    const slots = rejectedRowSlots(messages, [
      record({ key: 'early', record_timestamp: iso(500) }),
      record({ key: 'middle', record_timestamp: iso(1_500) }),
      record({ key: 'late', record_timestamp: iso(9_000) }),
    ]);
    expect(slots.get(0)?.map((r) => r.key)).toEqual(['early']);
    expect(slots.get(1)?.map((r) => r.key)).toEqual(['middle']);
    expect(slots.get(3)?.map((r) => r.key)).toEqual(['late']);
    expect(slots.size).toBe(3);
  });

  it('puts a row before a message of the same time', () => {
    const slots = rejectedRowSlots(messages, [record({ key: 'same', record_timestamp: iso(2_000) })]);
    expect(slots.get(1)?.map((r) => r.key)).toEqual(['same']);
    expect(slots.get(2)).toBeUndefined();
  });

  it('holds every row in the last slot when there are no messages', () => {
    const slots = rejectedRowSlots([], [
      record({ key: 'a', record_timestamp: iso(1) }),
      record({ key: 'b', record_timestamp: iso(2) }),
    ]);
    expect(slots.get(0)?.map((r) => r.key)).toEqual(['a', 'b']);
    expect(slots.size).toBe(1);
  });

  it('sends rows whose time is NaN to the last slot', () => {
    const slots = rejectedRowSlots(messages, [
      record({ key: 'notime', record_timestamp: 'garbage', first_seen: 'garbage', last_seen: 'garbage' }),
    ]);
    expect(slots.get(3)?.map((r) => r.key)).toEqual(['notime']);
    expect(slots.size).toBe(1);
  });

  it('orders a slot by time, then by key', () => {
    const slots = rejectedRowSlots(messages, [
      record({ key: 'b', record_timestamp: iso(1_500) }),
      record({ key: 'a', record_timestamp: iso(1_500) }),
      record({ key: 'c', record_timestamp: iso(1_200) }),
    ]);
    expect(slots.get(1)?.map((r) => r.key)).toEqual(['c', 'a', 'b']);
  });

  it('orders rows with no time after timed ones, by key', () => {
    const garbage = { record_timestamp: 'nope', first_seen: 'nope', last_seen: 'nope' };
    const slots = rejectedRowSlots(messages, [
      record({ key: 'zz', ...garbage }),
      record({ key: 'late', record_timestamp: iso(9_000) }),
      record({ key: 'aa', ...garbage }),
    ]);
    expect(slots.get(3)?.map((r) => r.key)).toEqual(['late', 'aa', 'zz']);
  });

  it('does not mutate its inputs', () => {
    const rows = [
      record({ key: 'b', record_timestamp: iso(1_500) }),
      record({ key: 'a', record_timestamp: iso(1_500) }),
    ];
    const rowsBefore = rows.map((r) => ({ ...r }));
    const msgs = [{ timestamp: 1_000 }, { timestamp: 2_000 }];
    rejectedRowSlots(msgs, rows);
    expect(rows).toEqual(rowsBefore);
    expect(msgs).toEqual([{ timestamp: 1_000 }, { timestamp: 2_000 }]);
  });
});
