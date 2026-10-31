import { describe, it, expect } from 'vitest';
import {
  blockConfirmQuestion,
  blockedIdsFromRules,
  blockedNotice,
  groupsWithMember,
  peerLabel,
  unblockedNotice,
} from './blockedPeers';

describe('blockedIdsFromRules', () => {
  it('reads blocked_nodes from the rules themselves and from the {rules} wrapper', () => {
    expect(blockedIdsFromRules({ blocked_nodes: ['dpc-node-a'] })).toEqual(['dpc-node-a']);
    expect(blockedIdsFromRules({ status: 'success', rules: { blocked_nodes: ['dpc-node-b'] } })).toEqual(['dpc-node-b']);
  });

  it('an empty list is an empty list, a missing key is "does not say"', () => {
    expect(blockedIdsFromRules({ blocked_nodes: [] })).toEqual([]);
    expect(blockedIdsFromRules({ message: 'saved' })).toBeNull();
    expect(blockedIdsFromRules(null)).toBeNull();
    expect(blockedIdsFromRules('x')).toBeNull();
  });

  it('drops entries that are not strings', () => {
    expect(blockedIdsFromRules({ blocked_nodes: ['dpc-node-a', 7, null] })).toEqual(['dpc-node-a']);
  });
});

describe('groupsWithMember', () => {
  it('counts the groups whose members include the node', () => {
    const groups = [
      { members: ['dpc-node-a', 'dpc-node-b'] },
      { members: ['dpc-node-b'] },
      { members: [] },
      {},
    ];
    expect(groupsWithMember(groups, 'dpc-node-b')).toBe(2);
    expect(groupsWithMember(groups, 'dpc-node-a')).toBe(1);
    expect(groupsWithMember(groups, 'dpc-node-z')).toBe(0);
  });
});

describe('peerLabel', () => {
  it('uses the name part of "name | id", else the short id', () => {
    expect(peerLabel('dpc-node-0123456789abcdef', 'Alice | dpc-node-0123456789abcdef')).toBe('Alice');
    expect(peerLabel('dpc-node-0123456789abcdef', 'dpc-node-0123456789abcdef')).toBe('dpc-node-01234567');
    expect(peerLabel('dpc-node-0123456789abcdef', undefined)).toBe('dpc-node-01234567');
  });
});

describe('the Block wording', () => {
  it('the question names the peer, the group count, the rule deletion and what Unblock does not do', () => {
    const q = blockConfirmQuestion('Alice', 2);
    expect(q).toContain('Block Alice?');
    expect(q).toContain('ends the connection');
    expect(q).toContain('2 groups');
    expect(q).toContain('deletes every firewall rule that names Alice');
    expect(q).toContain('does not put them back');
    expect(blockConfirmQuestion('Alice', 1)).toContain('1 group,');
  });

  it('the notice carries the counts the backend returned', () => {
    expect(blockedNotice('Alice', { removed_from_groups: ['g1', 'g2'], removed_rules: ['nodes.x'] }))
      .toBe('Blocked Alice. Removed from 2 groups, 1 firewall rule deleted.');
    expect(blockedNotice('Alice', {})).toBe('Blocked Alice. Removed from 0 groups, 0 firewall rules deleted.');
    expect(unblockedNotice('Alice')).toContain('groups and rules are not restored');
  });
});
