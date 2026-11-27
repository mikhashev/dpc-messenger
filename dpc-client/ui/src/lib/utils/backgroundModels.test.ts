import { describe, it, expect } from 'vitest';
import { allowedValue, EMBEDDING_DEVICES, GLINER_DEVICES, KG_BACKENDS } from './backgroundModels';

describe('the option lists', () => {
    it('are the backend readers own sets — the embedding model knows mps, GLiNER does not', () => {
        expect(EMBEDDING_DEVICES).toEqual(['auto', 'cpu', 'cuda', 'mps']);
        expect(GLINER_DEVICES).toEqual(['auto', 'cpu', 'cuda']);
        expect(KG_BACKENDS).toEqual(['sqlite', 'grafeo']);
    });
});

describe('allowedValue', () => {
    it('takes a value its list knows, lowercased and trimmed', () => {
        expect(allowedValue('cuda', GLINER_DEVICES, 'auto')).toBe('cuda');
        expect(allowedValue(' CUDA ', EMBEDDING_DEVICES, 'auto')).toBe('cuda');
        expect(allowedValue('Sqlite', KG_BACKENDS, 'sqlite')).toBe('sqlite');
        expect(allowedValue('mps', EMBEDDING_DEVICES, 'auto')).toBe('mps');
    });
    it('answers the fallback for anything a select must not hold', () => {
        // mps is legal for the embedding model and not for GLiNER — the guard reads
        // the list it is given, not the key it came from.
        expect(allowedValue('mps', GLINER_DEVICES, 'auto')).toBe('auto');
        expect(allowedValue('postgres', KG_BACKENDS, 'sqlite')).toBe('sqlite');
        expect(allowedValue('', EMBEDDING_DEVICES, 'auto')).toBe('auto');
        expect(allowedValue('  ', GLINER_DEVICES, 'auto')).toBe('auto');
        expect(allowedValue(5, EMBEDDING_DEVICES, 'auto')).toBe('auto');
        expect(allowedValue(null, KG_BACKENDS, 'sqlite')).toBe('sqlite');
        expect(allowedValue(undefined, EMBEDDING_DEVICES, 'auto')).toBe('auto');
    });
});
