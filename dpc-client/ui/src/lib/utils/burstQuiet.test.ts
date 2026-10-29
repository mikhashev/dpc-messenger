import { describe, it, expect } from 'vitest';
import { burstQuietFromAgent, burstQuietToSend } from './burstQuiet';

describe('burstQuietFromAgent', () => {
    it('shows the default for an agent whose row has no pause', () => {
        expect(burstQuietFromAgent(undefined)).toBe(20);
        expect(burstQuietFromAgent(null)).toBe(20);
    });
    it('shows the stored pause, and a stored 0 stays 0', () => {
        expect(burstQuietFromAgent(45)).toBe(45);
        expect(burstQuietFromAgent(0)).toBe(0);
    });
});

describe('burstQuietToSend', () => {
    it('sends 0 as 0, not as the default', () => {
        expect(burstQuietToSend(0)).toEqual({ value: 0 });
    });
    it('sends the ends of the range and what is between', () => {
        expect(burstQuietToSend(300)).toEqual({ value: 300 });
        expect(burstQuietToSend(45)).toEqual({ value: 45 });
        expect(burstQuietToSend('45')).toEqual({ value: 45 });
    });
    it('reads an emptied field as the default', () => {
        expect(burstQuietToSend(null)).toEqual({ value: 20 });
        expect(burstQuietToSend('')).toEqual({ value: 20 });
    });
    it('refuses what the backend would refuse', () => {
        for (const bad of [301, -1, NaN, 'abc']) {
            expect(burstQuietToSend(bad)).toHaveProperty('error');
        }
    });
});
