import { describe, it, expect } from 'vitest';
import { peerRowLabel } from './peerRowLabel';

describe('peerRowLabel', () => {
  it('drops the node id that follows a pipe', () => {
    expect(peerRowLabel('Alice (laptop) | dpc-node-0a1b2c3d4e5f60718293a4b5c6d7e8f9')).toBe('Alice (laptop)');
  });

  it('drops an id that was already cut with an ellipsis', () => {
    expect(peerRowLabel('Alice (laptop) | dpc-node-0a1b2c3d4e5…')).toBe('Alice (laptop)');
  });

  it('leaves a plain name alone', () => {
    expect(peerRowLabel('Alice')).toBe('Alice');
  });

  it('keeps a pipe that is not followed by a node id', () => {
    expect(peerRowLabel('Ops | night shift')).toBe('Ops | night shift');
  });

  it('a name that is only the id stays the id, not an empty row', () => {
    const id = 'dpc-node-0123456789abcdef0123456789abcdef';
    expect(peerRowLabel(id)).toBe(id);
    expect(peerRowLabel(` | ${id}`)).toBe(` | ${id}`);
  });
});
