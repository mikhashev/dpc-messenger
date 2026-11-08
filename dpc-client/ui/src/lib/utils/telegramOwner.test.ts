import { describe, it, expect } from 'vitest';
import { ownerUserIdFromAgent, ownerUserIdToSend } from './telegramOwner';

describe('ownerUserIdFromAgent', () => {
    it('shows an empty field for an agent whose row has no owner id', () => {
        expect(ownerUserIdFromAgent(undefined)).toBe('');
        expect(ownerUserIdFromAgent(null)).toBe('');
    });
    it('shows the stored id', () => {
        expect(ownerUserIdFromAgent('429727247')).toBe('429727247');
    });
});

describe('ownerUserIdToSend', () => {
    it('sends digits as they are, without surrounding spaces', () => {
        expect(ownerUserIdToSend('429727247')).toEqual({ value: '429727247' });
        expect(ownerUserIdToSend('  429727247 ')).toEqual({ value: '429727247' });
    });
    it('reads an emptied field as no owner', () => {
        expect(ownerUserIdToSend('')).toEqual({ value: null });
        expect(ownerUserIdToSend('   ')).toEqual({ value: null });
        expect(ownerUserIdToSend(null)).toEqual({ value: null });
        expect(ownerUserIdToSend(undefined)).toEqual({ value: null });
    });
    it('refuses what the backend would refuse', () => {
        for (const bad of ['abc', '-429727247', '+429727247', '42 97', '4.2', '@mike',
                           '４２９', '٤٢']) {
            expect(ownerUserIdToSend(bad)).toHaveProperty('error');
        }
    });
});
