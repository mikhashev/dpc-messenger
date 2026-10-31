import { describe, it, expect } from 'vitest';
import { blockedRunLabel, foldBlockedRuns, isBlockedAuthor } from './blockedRuns';

type Msg = { id: string; sender?: string; agentOwner?: string | null };

const BAD = 'dpc-node-bad';
const blocked = new Set([BAD]);
const fold = (messages: Msg[], set: ReadonlySet<string> = blocked) =>
  foldBlockedRuns(messages, (m) => isBlockedAuthor(set, m));

describe('foldBlockedRuns', () => {
  it('folds consecutive blocked messages into one run', () => {
    const rows = fold([
      { id: 'a', sender: 'dpc-node-ok' },
      { id: 'b', sender: BAD },
      { id: 'c', sender: BAD },
      { id: 'd', sender: BAD },
      { id: 'e', sender: 'dpc-node-ok' },
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['message', 'run', 'message']);
    const run = rows[1];
    expect(run.kind === 'run' && run.messages.map((m) => m.id)).toEqual(['b', 'c', 'd']);
    expect(run.kind === 'run' && run.startIndex).toBe(1);
  });

  it('a single blocked message is a run of one', () => {
    const rows = fold([{ id: 'a', sender: BAD }]);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind === 'run' && rows[0].messages).toHaveLength(1);
  });

  it('a blocked author between two normal messages splits nothing else', () => {
    const rows = fold([
      { id: 'a', sender: 'dpc-node-ok' },
      { id: 'b', sender: BAD },
      { id: 'c', sender: 'dpc-node-ok' },
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['message', 'run', 'message']);
    expect(rows[2].kind === 'message' && rows[2].index).toBe(2);
  });

  it('a normal message between two blocked ones makes two runs', () => {
    const rows = fold([
      { id: 'a', sender: BAD },
      { id: 'b', sender: 'dpc-node-ok' },
      { id: 'c', sender: BAD },
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['run', 'message', 'run']);
    expect(rows[2].kind === 'run' && rows[2].startIndex).toBe(2);
  });

  it('an empty block list folds nothing', () => {
    const rows = fold(
      [{ id: 'a', sender: BAD }, { id: 'b', sender: BAD }],
      new Set(),
    );
    expect(rows.map((r) => r.kind)).toEqual(['message', 'message']);
  });

  it('messages with no sender are never folded', () => {
    const rows = fold([
      { id: 'a' },
      { id: 'b', sender: '' },
      { id: 'c', sender: null as unknown as string },
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['message', 'message', 'message']);
  });

  it('an agent owned by a blocked node is folded with the node\'s own messages', () => {
    const rows = fold([
      { id: 'a', sender: BAD },
      { id: 'b', sender: 'agent_x', agentOwner: BAD },
      { id: 'c', sender: 'agent_y', agentOwner: 'dpc-node-ok' },
    ]);
    expect(rows.map((r) => r.kind)).toEqual(['run', 'message']);
    expect(rows[0].kind === 'run' && rows[0].messages).toHaveLength(2);
  });

  it('keeps every message, in order', () => {
    const input: Msg[] = [
      { id: '1', sender: 'x' }, { id: '2', sender: BAD }, { id: '3', sender: BAD }, { id: '4', sender: 'x' },
    ];
    const ids = fold(input).flatMap((r) => (r.kind === 'run' ? r.messages : [r.message])).map((m) => m.id);
    expect(ids).toEqual(['1', '2', '3', '4']);
  });
});

describe('blockedRunLabel', () => {
  it('says "1 message" in the singular and "N messages" otherwise', () => {
    expect(blockedRunLabel(1)).toBe('1 message from a blocked node');
    expect(blockedRunLabel(3)).toBe('3 messages from a blocked node');
  });
});
