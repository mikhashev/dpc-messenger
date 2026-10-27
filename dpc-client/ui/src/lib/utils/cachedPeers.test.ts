import { describe, it, expect } from 'vitest';
import {
  cachedPeerLine,
  cachedPeerLines,
  cachedPeerName,
  cachedPeerSeen,
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
});
