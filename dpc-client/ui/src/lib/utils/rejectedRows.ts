// src/lib/utils/rejectedRows.ts
// Pure helpers for the rejected-record row (DPC #111): the words a refusal
// reason turns into, the epoch ms a row is placed by, and the slot of the
// message list a row belongs in front of. No Svelte, no stores — the row's
// component and its tests share these.

import type { RejectedRecord } from '$lib/types';

// The seven reasons this node refuses a record for, as words a person can read
// on the row. The codes are the backend's; two of them (unsigned,
// unknown_preimage) are refusals by this node's own setting, not accusations
// against the sender, and their words say so. Neither is `conflict`: nothing
// is wrong with that record on its own — what is wrong is that another content
// is already held under its id, and which side moved is not for this node to say.
export const REJECTED_REASON_TEXT: Record<RejectedRecord['reason'], string> = {
  signer_mismatch: 'signed by one node, attributed to another',
  content_hash_mismatch: 'content does not match its hash',
  signature_check_failed: 'signature check failed',
  invalid_signature: 'invalid signature',
  unsigned: "no signature, refused by this node's setting",
  unknown_preimage: "signed over a format this node cannot check, refused by this node's setting",
  conflict: 'the same id arrived with different content; the copy held here is kept',
};

/** The words for a reason the UI knows; an unknown code shows itself (a reason
 *  a later backend adds must not read as a blank line), and no reason at all
 *  reads as the row's own word. Never empty. */
export function rejectedReasonText(reason: RejectedRecord['reason'] | string | null | undefined): string {
  if (!reason) return 'rejected';
  const known = REJECTED_REASON_TEXT[reason as RejectedRecord['reason']];
  return known ?? reason;
}

/** One ISO string to epoch ms, or null when it is absent or garbage — a record
 *  whose signature is wrong cannot be trusted to carry a parseable time. */
function parseIsoMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

/** Epoch ms the row is placed by: the record's own time when it parses, else
 *  when this node first saw it, else when it last saw it — never the clock,
 *  which would drag an old row to the bottom on every render. NaN when
 *  nothing parses; such a row goes to the last slot rather than to a place
 *  the record's own time does not support. */
export function rejectedRecordTime(record: RejectedRecord): number {
  return parseIsoMs(record.record_timestamp)
    ?? parseIsoMs(record.first_seen)
    ?? parseIsoMs(record.last_seen)
    ?? NaN;
}

/** Slot i holds the rows that go immediately BEFORE message i — a row whose
 *  time is at or before that message's and after the previous message's, so a
 *  row shares a message's time rather than passing it. Slot messages.length
 *  holds the rows after the last message, and all of them when there are no
 *  messages. Rows whose time is NaN go to the last slot: an unknown time is
 *  not "long ago". Within a slot, rows are ordered by time, then by key. The
 *  inputs are never mutated. */
export function rejectedRowSlots(
  messages: ReadonlyArray<{ timestamp: number }>,
  records: ReadonlyArray<RejectedRecord>,
): Map<number, RejectedRecord[]> {
  const slots = new Map<number, RejectedRecord[]>();
  const add = (slot: number, record: RejectedRecord) => {
    const rows = slots.get(slot);
    if (rows) rows.push(record);
    else slots.set(slot, [record]);
  };
  for (const record of records) {
    const ms = rejectedRecordTime(record);
    if (Number.isNaN(ms)) {
      add(messages.length, record);
      continue;
    }
    let slot = messages.length;
    for (let i = 0; i < messages.length; i++) {
      if (ms <= messages[i].timestamp) {
        slot = i;
        break;
      }
    }
    add(slot, record);
  }
  for (const rows of slots.values()) {
    // A row with no time sorts as "last" within the slot (it can only be in
    // the last one anyway); two such rows order by key, as equal times do.
    const timeOf = (r: RejectedRecord): number => {
      const ms = rejectedRecordTime(r);
      return Number.isNaN(ms) ? Number.POSITIVE_INFINITY : ms;
    };
    rows.sort((a, b) =>
      (timeOf(a) - timeOf(b)) || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
  }
  return slots;
}
