import { describe, it, expect } from 'vitest';
import { parseFigure, parseFigureAbove, parseNameList, parseWholeNumber } from './voiceSettings';

describe('parseWholeNumber', () => {
    it('takes a whole number at or above the floor, the floor included', () => {
        expect(parseWholeNumber('300', 0)).toBe(300);
        expect(parseWholeNumber(' 45 ', 0)).toBe(45);
        expect(parseWholeNumber('0', 0)).toBe(0);
        expect(parseWholeNumber(12, 1)).toBe(12);
        expect(parseWholeNumber('1', 1)).toBe(1);
    });
    it('refuses what the backend would refuse', () => {
        for (const bad of ['', '  ', '-1', '1.5', '1e3', 'soon', '5 s', '+5']) {
            expect(parseWholeNumber(bad, 0), JSON.stringify(bad)).toBeNull();
        }
        expect(parseWholeNumber('0', 1)).toBeNull();
        expect(parseWholeNumber(-1, 0)).toBeNull();
        expect(parseWholeNumber(2.5, 0)).toBeNull();
        expect(parseWholeNumber(NaN, 0)).toBeNull();
        expect(parseWholeNumber(undefined, 0)).toBeNull();
        expect(parseWholeNumber(null, 0)).toBeNull();
    });
});

describe('parseFigure', () => {
    it('takes decimals at or above the floor, the floor included', () => {
        expect(parseFigure('300', 0)).toBe(300);
        expect(parseFigure('1.5', 0)).toBe(1.5);
        expect(parseFigure(' 2.25 ', 0)).toBe(2.25);
        expect(parseFigure(0.5, 0)).toBe(0.5);
        expect(parseFigure('0', 0)).toBe(0);
    });
    it('refuses what the backend would refuse', () => {
        for (const bad of ['', ' ', '-1', '1e3', 'soon', '1,5', 'Infinity']) {
            expect(parseFigure(bad, 0), JSON.stringify(bad)).toBeNull();
        }
        expect(parseFigure('-0.5', 0)).toBeNull();
        expect(parseFigure(NaN, 0)).toBeNull();
        expect(parseFigure(undefined, 0)).toBeNull();
        expect(parseFigure(null, 0)).toBeNull();
    });
});

describe('parseFigureAbove', () => {
    it('takes a figure strictly above the floor', () => {
        expect(parseFigureAbove('1.5', 0)).toBe(1.5);
        expect(parseFigureAbove('600', 0)).toBe(600);
        expect(parseFigureAbove('0.1', 0)).toBe(0.1);
    });
    it('refuses the floor itself and everything below', () => {
        expect(parseFigureAbove('0', 0)).toBeNull();
        expect(parseFigureAbove('1', 1)).toBeNull();
        expect(parseFigureAbove('-2', 0)).toBeNull();
        expect(parseFigureAbove('', 0)).toBeNull();
    });
});

describe('parseNameList', () => {
    it('trims each entry and drops the empties', () => {
        expect(parseNameList('a, b , c')).toEqual(['a', 'b', 'c']);
        expect(parseNameList('whisper-large-v3-turbo,whisper-small, openai'))
            .toEqual(['whisper-large-v3-turbo', 'whisper-small', 'openai']);
        expect(parseNameList(' audio/webm ,audio/ogg, ,audio/wav '))
            .toEqual(['audio/webm', 'audio/ogg', 'audio/wav']);
    });
    it('is empty for an empty field, which the form refuses before sending', () => {
        expect(parseNameList('')).toEqual([]);
        expect(parseNameList(' , , ')).toEqual([]);
        expect(parseNameList(undefined)).toEqual([]);
        expect(parseNameList(null)).toEqual([]);
    });
});
