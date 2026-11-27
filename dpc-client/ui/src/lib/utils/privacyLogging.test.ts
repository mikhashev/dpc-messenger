import { describe, it, expect } from 'vitest';
import { allowedValue, boolValue, isModuleName, LOG_LEVELS } from './privacyLogging';

describe('the option lists', () => {
    it('are the backend reader own set — five levels, no NOTSET, no WARN', () => {
        expect(LOG_LEVELS).toEqual(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']);
    });
});

describe('allowedValue', () => {
    it('takes a level its list knows, uppercased and trimmed', () => {
        expect(allowedValue('debug', LOG_LEVELS, 'INFO')).toBe('DEBUG');
        expect(allowedValue('  Warning ', LOG_LEVELS, 'INFO')).toBe('WARNING');
        expect(allowedValue('CRITICAL', LOG_LEVELS, 'INFO')).toBe('CRITICAL');
    });
    it('answers the fallback for anything a select must not hold', () => {
        // NOTSET is an attribute of Python's logging module and not a level this
        // build's reader documents — the guard reads the list, not the module.
        expect(allowedValue('NOTSET', LOG_LEVELS, 'INFO')).toBe('INFO');
        expect(allowedValue('WARN', LOG_LEVELS, 'INFO')).toBe('INFO');
        expect(allowedValue('', LOG_LEVELS, 'INFO')).toBe('INFO');
        expect(allowedValue(5, LOG_LEVELS, 'INFO')).toBe('INFO');
        expect(allowedValue(null, LOG_LEVELS, 'INFO')).toBe('INFO');
        expect(allowedValue(undefined, LOG_LEVELS, 'INFO')).toBe('INFO');
    });
});

describe('boolValue', () => {
    it('answers a real bool and the fallback for anything else', () => {
        expect(boolValue(true, false)).toBe(true);
        expect(boolValue(false, true)).toBe(false);
        expect(boolValue('true', false)).toBe(false);
        expect(boolValue(1, false)).toBe(false);
        expect(boolValue(null, true)).toBe(true);
        expect(boolValue(undefined, true)).toBe(true);
    });
});

describe('isModuleName', () => {
    it('takes dotted logger names and refuses what the writer refuses', () => {
        expect(isModuleName('httpx')).toBe(true);
        expect(isModuleName('telegram.ext.Application')).toBe(true);
        expect(isModuleName(' my.module ')).toBe(true);
        expect(isModuleName('')).toBe(false);
        expect(isModuleName('  ')).toBe(false);
        expect(isModuleName('bad name')).toBe(false);
        expect(isModuleName('a..b')).toBe(false);
        expect(isModuleName('.a')).toBe(false);
        expect(isModuleName('a.')).toBe(false);
        expect(isModuleName('9lives')).toBe(false);
    });
});
