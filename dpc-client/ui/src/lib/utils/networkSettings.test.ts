import { describe, it, expect } from 'vitest';
import {
    allowedValue, boolValue, credentialPlaceholder, parseFigure,
    parseFigureAbove, parseHostPortList, parseIceList, parsePort, parseWholeNumber,
} from './networkSettings';

describe('parsePort (the four port keys)', () => {
    it('takes whole numbers 1..65535, as text or number', () => {
        expect(parsePort('8889', 1)).toBe(8889);
        expect(parsePort(' 9997 ', 1)).toBe(9997);
        expect(parsePort(65535, 1)).toBe(65535);
    });
    it('answers null for what the backend would refuse, so the form says so first', () => {
        expect(parsePort('', 1)).toBeNull();
        expect(parsePort('0', 1)).toBeNull();
        expect(parsePort('70000', 1)).toBeNull();
        expect(parsePort('2.5', 1)).toBeNull();
        expect(parsePort('nine', 1)).toBeNull();
        expect(parsePort(true as unknown as string, 1)).toBeNull();
    });
});

describe('parseWholeNumber (the counts)', () => {
    it('takes whole numbers at or above the reader minimum', () => {
        expect(parseWholeNumber('20', 1)).toBe(20);
        expect(parseWholeNumber(8, 1)).toBe(8);
    });
    it('answers null for zero, negatives, decimals and text', () => {
        expect(parseWholeNumber('0', 1)).toBeNull();
        expect(parseWholeNumber('-1', 1)).toBeNull();
        expect(parseWholeNumber('1.5', 1)).toBeNull();
        expect(parseWholeNumber('', 1)).toBeNull();
    });
});

describe('the figure guards (timeouts, delays, thresholds)', () => {
    it('parseFigureAbove refuses the 0 a timeout must not be', () => {
        expect(parseFigureAbove('30', 0)).toBe(30);
        expect(parseFigureAbove('0.5', 0)).toBe(0.5);
        expect(parseFigureAbove('0', 0)).toBeNull();
        expect(parseFigureAbove('-1', 0)).toBeNull();
        expect(parseFigureAbove('soon', 0)).toBeNull();
    });
    it('parseFigure takes the honest zeros (no delay, no pause, no threshold)', () => {
        expect(parseFigure('0', 0)).toBe(0);
        expect(parseFigure('10.5', 0)).toBe(10.5);
        expect(parseFigure('-0.1', 0)).toBeNull();
        expect(parseFigure('', 0)).toBeNull();
    });
});

describe('parseIceList (the STUN/TURN server lists)', () => {
    it('takes comma-separated stun/turn/turns addresses, with ports and queries', () => {
        expect(parseIceList('stun:stun.l.google.com:19302')).toEqual(['stun:stun.l.google.com:19302']);
        expect(parseIceList('turn:a.example.com:80, turn:b.example.com:443?transport=tcp'))
            .toEqual(['turn:a.example.com:80', 'turn:b.example.com:443?transport=tcp']);
        expect(parseIceList('turns:x.example.com')).toEqual(['turns:x.example.com']);
        expect(parseIceList('')).toEqual([]);
    });
    it('answers null for an entry aiortc could not parse', () => {
        expect(parseIceList('http://a.example.com')).toBeNull();
        expect(parseIceList('not a url')).toBeNull();
        expect(parseIceList('turn:')).toBeNull();
        expect(parseIceList('turn:h:99999')).toBeNull();
        expect(parseIceList('turn:h:0')).toBeNull();
    });
});

describe('parseHostPortList (the DHT seed nodes)', () => {
    it('takes comma-separated host:port entries', () => {
        expect(parseHostPortList('203.0.113.7:8889')).toEqual(['203.0.113.7:8889']);
        expect(parseHostPortList('a.example.org:8889, [::1]:8889'))
            .toEqual(['a.example.org:8889', '[::1]:8889']);
        expect(parseHostPortList('')).toEqual([]);
    });
    it('answers null for an entry get_dht_seed_nodes would silently skip', () => {
        expect(parseHostPortList('nohost')).toBeNull();
        expect(parseHostPortList('host:notaport')).toBeNull();
        expect(parseHostPortList(':8889')).toBeNull();
        expect(parseHostPortList('host:0')).toBeNull();
    });
});

describe('boolValue and allowedValue', () => {
    it('answers a real bool and the fallback for anything else', () => {
        expect(boolValue(true, false)).toBe(true);
        expect(boolValue('true', false)).toBe(false);
        expect(boolValue(1, false)).toBe(false);
    });
    it('keeps a select on one of its options whatever a command answered', () => {
        expect(allowedValue('0.0.0.0', ['dual', '0.0.0.0', '::'], 'dual')).toBe('0.0.0.0');
        expect(allowedValue('weird', ['dual', '0.0.0.0', '::'], 'dual')).toBe('dual');
        expect(allowedValue(undefined, ['google', 'github'], 'google')).toBe('google');
    });
});

describe('credentialPlaceholder', () => {
    it('says a credential is stored with dots, never a value, and that empty keeps it', () => {
        const set = credentialPlaceholder(true);
        expect(set).toContain('••••••••');
        expect(set).toContain('(set');
        expect(set).toContain('leave empty to keep it');
    });
    it('asks for the credential when none is stored', () => {
        expect(credentialPlaceholder(false)).toBe('Paste the TURN credential');
    });
});
