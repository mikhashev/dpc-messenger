/**
 * A second group event merges into the held record instead of replacing it
 * (A-GROUP-INVITE-EVENT-CARRIES-NO-AGENTS-AND-AN-OFFLINE-ADD-NEVER-ARRIVES:
 * a members-only invite left the @-list with one name until a reload).
 */

import { describe, it, expect } from 'vitest';
import { mergeGroupEvent } from './groupEvents';
import { mentionableMembers } from './mentionableMembers';
import type { GroupChat } from '$lib/types';

const SELF = 'dpc-node-self';
const CREATOR = 'dpc-node-creator';

const held: GroupChat = {
  group_id: 'group-0123456789ab',
  name: 'work',
  created_by: CREATOR,
  members: [CREATOR, SELF],
  agents: { [CREATOR]: ['agent_001'] },
  agent_names: { [CREATOR]: { agent_001: 'Ark' } },
  version: 22,
};

describe('mergeGroupEvent', () => {
  it('keeps the agents a later members-only event does not carry', () => {
    const second = {
      group_id: held.group_id, name: 'work', created_by: CREATOR,
      members: [CREATOR, SELF, 'dpc-node-third'],
    } as GroupChat;

    const merged = mergeGroupEvent(new Map([[held.group_id, held]]), second);

    const group = merged.get(held.group_id)!;
    expect(group.members).toEqual([CREATOR, SELF, 'dpc-node-third']);
    expect(group.agents).toEqual(held.agents);
    const names = mentionableMembers(group, SELF, new Map(), () => undefined).map(m => m.mention_name);
    expect(names).toContain('Ark');
  });

  it('lets an event that carries agents replace the held ones', () => {
    const second = { ...held, agents: { [CREATOR]: ['agent_002'] }, version: 23 };

    const group = mergeGroupEvent(new Map([[held.group_id, held]]), second).get(held.group_id)!;

    expect(group.agents).toEqual({ [CREATOR]: ['agent_002'] });
    expect(group.version).toBe(23);
  });

  it('ignores an event older than the held entry', () => {
    const older = { ...held, name: 'renamed long ago', agents: {}, version: 20 };

    const store = new Map([[held.group_id, held]]);
    const after = mergeGroupEvent(store, older);

    expect(after.get(held.group_id)).toEqual(held);
  });

  it('applies an event of the same version: the backend settled the tie before sending it', () => {
    const same = { group_id: held.group_id, topic: 'set here', version: 22 };

    const group = mergeGroupEvent(new Map([[held.group_id, held]]), same).get(held.group_id)!;

    expect(group.topic).toBe('set here');
    expect(group.agents).toEqual(held.agents);
  });

  it('ignores a member leaving that is older than the held roster', () => {
    const left = { group_id: held.group_id, members: [CREATOR], version: 21 };

    const group = mergeGroupEvent(new Map([[held.group_id, held]]), left).get(held.group_id)!;

    expect(group.members).toEqual(held.members);
  });

  it('adds nothing from a partial event about a group not held', () => {
    const before = new Map<string, GroupChat>();

    const after = mergeGroupEvent(before, { group_id: held.group_id, topic: 'x', version: 3 });

    expect(after.has(held.group_id)).toBe(false);
  });

  it('does not bring a deleted room back from a late partial event', () => {
    const store = new Map([[held.group_id, held]]);
    store.delete(held.group_id); // what group_deleted does in coreService

    const after = mergeGroupEvent(store, { group_id: held.group_id, topic: 'late', version: 20 });

    expect(after.has(held.group_id)).toBe(false);
  });

  it('adds a whole record of a group with no creator', () => {
    const noCreator = { ...held, created_by: '' };

    const after = mergeGroupEvent(new Map<string, GroupChat>(), noCreator);

    expect(after.get(held.group_id)?.created_by).toBe('');
  });

  it('adds a group not held and leaves the store it was given untouched', () => {
    const before = new Map<string, GroupChat>();

    const after = mergeGroupEvent(before, held);

    expect(after.get(held.group_id)).toEqual(held);
    expect(before.size).toBe(0);
  });
});
