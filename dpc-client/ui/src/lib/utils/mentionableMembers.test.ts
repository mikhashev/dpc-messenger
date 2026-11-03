import { describe, it, expect } from 'vitest';
import { mentionableMembers } from './mentionableMembers';

const SELF = 'dpc-node-aaaa';
const GONE = 'dpc-node-bbbb';
const STAYS = 'dpc-node-cccc';

// The shape the live record has had since 2026-10-31: a departed key in both maps.
const group = {
  members: [SELF, STAYS],
  agents: {
    [SELF]: ['agent_001'],
    [GONE]: ['agent_ubu_acbf15fb', 'ext:CC_win10'],
    [STAYS]: ['agent_win_1', 'ext:CC_c'],
  },
  agent_names: {
    [SELF]: { agent_001: 'Ark' },
    [GONE]: { agent_ubu_acbf15fb: 'Ubu', 'ext:CC_win10': 'CC_win10' },
    [STAYS]: { agent_win_1: 'Warren', 'ext:CC_c': 'CC_c' },
  },
};
const peers = new Map([[STAYS, 'Mike (linux) | dpc-node-cccc'], [GONE, 'Win10 box | dpc-node-bbbb']]);
const noLocal = () => undefined;
const names = (list: { mention_name: string }[]) => list.map(m => m.mention_name);

describe('mentionableMembers', () => {
  it('does not offer the agents of a node that is not in members', () => {
    const offered = names(mentionableMembers(group, SELF, peers, noLocal));
    expect(offered).not.toContain('Ubu');
    expect(offered).not.toContain('CC_win10');
  });

  it('offers the agents of a node that is in members, with their owner', () => {
    const list = mentionableMembers(group, SELF, peers, noLocal);
    expect(list.find(m => m.node_id === 'agent_win_1')).toEqual(
      { node_id: 'agent_win_1', name: 'Warren (Mike (linux))', mention_name: 'Warren' });
    expect(names(list)).toContain('CC_c');
    expect(list.find(m => m.node_id === 'agent_001')?.name).toBe('Ark (You)');
  });

  it('offers the humans of members only, this node left out', () => {
    const humans = mentionableMembers(group, SELF, peers, noLocal).filter(m => m.node_id.startsWith('dpc-node-'));
    expect(humans.map(m => m.node_id)).toEqual([STAYS]);
  });

  it('offers a departed node\'s agents again once the node is back in members', () => {
    const back = { ...group, members: [SELF, STAYS, GONE] };
    const offered = names(mentionableMembers(back, SELF, peers, noLocal));
    expect(offered).toContain('Ubu');
    expect(offered).toContain('CC_win10');
  });

  it('a copy that does not list its own node offers none of its own agents', () => {
    const removed = { ...group, members: [STAYS] };
    const offered = names(mentionableMembers(removed, SELF, peers, noLocal));
    expect(offered).not.toContain('Ark');
    expect(offered).toContain('Warren');
  });

  it('a name from this node\'s own agent list wins over the group\'s agent_names', () => {
    const local = (id: string) => (id === 'agent_001' ? 'Ark (renamed)' : undefined);
    expect(names(mentionableMembers(group, SELF, peers, local))).toContain('Ark (renamed)');
  });

  it('answers an empty list for no group, and for a group with no members', () => {
    expect(mentionableMembers(null, SELF, peers, noLocal)).toEqual([]);
    expect(mentionableMembers({ agents: group.agents }, SELF, peers, noLocal)).toEqual([]);
  });
});
