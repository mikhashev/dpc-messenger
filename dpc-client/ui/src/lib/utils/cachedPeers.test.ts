import { describe, it, expect } from 'vitest';
import {
  cachedPeerLine,
  cachedPeerLines,
  cachedPeerName,
  cachedPeerSeen,
  cachedPeersRemovable,
  cachedPeersRemoveAllQuestion,
} from './cachedPeers';

describe('cachedPeers', () => {
  it('a peer cached without a name is shown as unnamed, not as an empty cell', () => {
    expect(cachedPeerName({ node_id: 'dpc-node-a', display_name: null })).toBe('(no name)');
    expect(cachedPeerName({ node_id: 'dpc-node-a', display_name: '   ' })).toBe('(no name)');
    expect(cachedPeerName({ node_id: 'dpc-node-a' })).toBe('(no name)');
    expect(cachedPeerName({ node_id: 'dpc-node-a', display_name: ' Alice ' })).toBe('Alice');
  });

  it('a copied line holds the name and the whole node id', () => {
    const id = 'dpc-node-0123456789abcdef0123456789abcdef';
    expect(cachedPeerLine({ node_id: id, display_name: 'Alice' })).toBe(`Alice ${id}`);
  });

  it('copy-all keeps the order of the list, one peer per line', () => {
    const rows = [
      { node_id: 'dpc-node-b', display_name: 'Bob' },
      { node_id: 'dpc-node-a', display_name: null },
    ];
    expect(cachedPeerLines(rows)).toBe('Bob dpc-node-b\n(no name) dpc-node-a');
    expect(cachedPeerLines([])).toBe('');
  });

  it('the last-seen day is read off the timestamp, and a missing or broken one is empty', () => {
    expect(cachedPeerSeen({ node_id: 'x', last_seen: '2026-10-26T23:59:59+00:00' })).toBe('2026-10-26');
    expect(cachedPeerSeen({ node_id: 'x', last_seen: null })).toBe('');
    expect(cachedPeerSeen({ node_id: 'x', last_seen: 'not a date' })).toBe('');
  });

  it('remove-all counts only the peers that are not connected now', () => {
    const rows = [
      { node_id: 'a', is_connected: true },
      { node_id: 'b', is_connected: false },
      { node_id: 'c' },
    ];
    expect(cachedPeersRemovable(rows).map((r) => r.node_id)).toEqual(['b', 'c']);
    expect(cachedPeersRemoveAllQuestion(rows)).toBe('Remove 2 peers? Connected peers stay.');
  });

  it('remove-all says "peer" for one, and asks nothing when nothing could be removed', () => {
    expect(cachedPeersRemoveAllQuestion([{ node_id: 'a' }])).toBe(
      'Remove 1 peer? Connected peers stay.',
    );
    expect(cachedPeersRemoveAllQuestion([{ node_id: 'a', is_connected: true }])).toBe('');
    expect(cachedPeersRemoveAllQuestion([])).toBe('');
  });
});
