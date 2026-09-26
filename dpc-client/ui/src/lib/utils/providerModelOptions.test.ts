import { describe, it, expect } from 'vitest';
import { groupModels, modelOptionLabel } from './providerModelOptions';

describe('modelOptionLabel', () => {
    it('names a -noreason twin as the no-thinking choice', () => {
        expect(modelOptionLabel({ id: 'qwen3.8-27b-noreason', kind: 'chat' })).toBe('qwen3.8-27b-noreason (no thinking)');
    });
    it('leaves any other id as it is', () => {
        expect(modelOptionLabel({ id: 'qwen3.8-27b', kind: 'chat' })).toBe('qwen3.8-27b');
        expect(modelOptionLabel({ id: 'bge-m3', kind: 'embedding' })).toBe('bge-m3');
    });
});

describe('groupModels', () => {
    it('keeps the backend order and groups by kind', () => {
        const groups = groupModels([
            { id: 'qwen3.8-27b', kind: 'chat' },
            { id: 'qwen3.8-27b-noreason', kind: 'chat' },
            { id: 'bge-m3', kind: 'embedding' },
            { id: 'whisper-1', kind: 'stt' },
            { id: 'odd', kind: 'vision-only' },
        ]);
        expect(groups.map((g) => g.label)).toEqual(['Chat', 'Embeddings', 'Speech to text', 'vision-only']);
        expect(groups[0].models.map((m) => m.id)).toEqual(['qwen3.8-27b', 'qwen3.8-27b-noreason']);
    });
    it('answers an empty list for nothing loaded', () => {
        expect(groupModels(null)).toEqual([]);
        expect(groupModels([])).toEqual([]);
    });
});
