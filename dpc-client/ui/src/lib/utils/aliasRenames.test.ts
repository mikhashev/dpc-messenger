import { describe, it, expect } from 'vitest';
import { trackRename } from './aliasRenames';

describe('the rename a reference still has to follow', () => {
  it('names the alias the references currently carry', () => {
    expect(trackRename({}, 'llama.cpp-abl', 'qwen3.8 27b Mythos')).toEqual({
      'llama.cpp-abl': 'qwen3.8 27b Mythos'
    });
  });

  it('collapses a chain back to the name on disk, not the last one typed', () => {
    let pending = trackRename({}, 'a', 'b');
    pending = trackRename(pending, 'b', 'c');

    expect(pending).toEqual({ a: 'c' });
  });

  it('drops a rename that came back to where it started', () => {
    let pending = trackRename({}, 'a', 'b');
    pending = trackRename(pending, 'b', 'a');

    expect(pending).toEqual({});
  });

  it('keeps two providers renamed in one session apart', () => {
    let pending = trackRename({}, 'a', 'b');
    pending = trackRename(pending, 'x', 'y');

    expect(pending).toEqual({ a: 'b', x: 'y' });
  });

  it('records nothing when the alias did not change', () => {
    expect(trackRename({}, 'a', 'a')).toEqual({});
  });

  it('records nothing when a name is missing', () => {
    expect(trackRename({}, '', 'b')).toEqual({});
    expect(trackRename({}, 'a', '')).toEqual({});
  });

  it('does not mutate the map it was given', () => {
    const pending = { a: 'b' };

    trackRename(pending, 'b', 'c');

    expect(pending).toEqual({ a: 'b' });
  });
});
