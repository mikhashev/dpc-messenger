import { describe, it, expect } from 'vitest';
import { envNameOrUndefined, isEnvVarName, looksLikeKey } from './apiKeyEnv';

describe('looksLikeKey', () => {
    it('flags a NeuralDeep key pasted where a variable name belongs', () => {
        expect(looksLikeKey('sk-L0abcDEF123')).toBe(true);
        expect(looksLikeKey('  sk-abc  ')).toBe(true);
        expect(looksLikeKey('1STARTS_WITH_DIGIT')).toBe(true);
    });
    it('leaves variable names and an empty field alone', () => {
        expect(looksLikeKey('NEURALDEEP_API_KEY')).toBe(false);
        expect(looksLikeKey('_private2')).toBe(false);
        expect(looksLikeKey('')).toBe(false);
        expect(looksLikeKey(undefined)).toBe(false);
    });
});

describe('isEnvVarName / envNameOrUndefined', () => {
    it('sends only a valid name', () => {
        expect(isEnvVarName('ZAI_API_KEY')).toBe(true);
        expect(isEnvVarName('sk-x')).toBe(false);
        expect(envNameOrUndefined(' NEURALDEEP_API_KEY ')).toBe('NEURALDEEP_API_KEY');
        expect(envNameOrUndefined('sk-x')).toBeUndefined();
        expect(envNameOrUndefined('')).toBeUndefined();
    });
});
