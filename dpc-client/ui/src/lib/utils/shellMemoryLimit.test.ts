import { describe, it, expect } from 'vitest';
import {
    MEMORY_LIMIT_ENV_NAMES,
    MEMORY_LIMIT_WARN_BELOW_MIB,
    formatMemoryLimit,
    memoryLimitInputText,
    memoryLimitView,
    memoryLimitWarning,
    parseMemoryLimitInput,
} from './shellMemoryLimit';

describe('formatMemoryLimit', () => {
    it('groups thousands and names the unit', () => {
        expect(formatMemoryLimit(32768)).toBe('32,768 MiB');
        expect(formatMemoryLimit(512)).toBe('512 MiB');
    });
    it('says no limit for 0, and for a hand-edited negative, which runs as no limit', () => {
        expect(formatMemoryLimit(0)).toBe('no limit');
        expect(formatMemoryLimit(-1)).toBe('no limit');
    });
});

describe('memoryLimitWarning', () => {
    it('warns below 1,024 MiB, in the agreed words', () => {
        expect(memoryLimitWarning(512)).toBe('Below 1,024 MiB: every agent command that grows past it is stopped.');
        expect(memoryLimitWarning(1)).not.toBe('');
        expect(memoryLimitWarning(MEMORY_LIMIT_WARN_BELOW_MIB - 1)).not.toBe('');
    });
    it('does not warn at 1,024 and above, nor for 0, which is no limit', () => {
        expect(memoryLimitWarning(1024)).toBe('');
        expect(memoryLimitWarning(8192)).toBe('');
        expect(memoryLimitWarning(0)).toBe('');
    });
});

describe('parseMemoryLimitInput', () => {
    // Zcode, 2026-11-02: emptying the field puts the default back; only a typed 0 is no limit.
    it('reads an empty field as the default 8,192 MiB, not as no limit', () => {
        expect(parseMemoryLimitInput('')).toEqual({ mb: 8192 });
        expect(parseMemoryLimitInput('   ')).toEqual({ mb: 8192 });
        expect(parseMemoryLimitInput(null)).toEqual({ mb: 8192 });
        expect(parseMemoryLimitInput(undefined)).toEqual({ mb: 8192 });
    });
    it('keeps a typed 0 as 0', () => {
        expect(parseMemoryLimitInput('0')).toEqual({ mb: 0 });
        expect(parseMemoryLimitInput(0)).toEqual({ mb: 0 });
    });
    it('reads whole numbers, with or without thousands commas and spaces around', () => {
        expect(parseMemoryLimitInput('8192')).toEqual({ mb: 8192 });
        expect(parseMemoryLimitInput(' 32,768 ')).toEqual({ mb: 32768 });
        expect(parseMemoryLimitInput(4096)).toEqual({ mb: 4096 });
    });
    it('refuses a negative, a fraction and text, with a reason', () => {
        for (const bad of ['-1', '-4096', '1.5', '8 GB', 'lots', '1e3', 1.5, -2]) {
            const got = parseMemoryLimitInput(bad as any);
            expect(got, String(bad)).toHaveProperty('error');
            expect((got as { error: string }).error).toContain('whole number');
        }
    });
    it('refuses a number too large to be exact', () => {
        expect(parseMemoryLimitInput('99999999999999999999')).toHaveProperty('error');
    });
});

describe('memoryLimitInputText', () => {
    it('opens with 0 for no limit and with the number otherwise', () => {
        expect(memoryLimitInputText(0)).toBe('0');
        expect(memoryLimitInputText(8192)).toBe('8192');
    });
});

describe('memoryLimitView', () => {
    it('is null when the backend sent no limit', () => {
        expect(memoryLimitView(null)).toBeNull();
        expect(memoryLimitView({})).toBeNull();
        expect(memoryLimitView({ shell_memory_limit_mb: null })).toBeNull();
    });
    it('words the line from the number', () => {
        const v = memoryLimitView({ shell_memory_limit_mb: 32768, shell_memory_limit_source: 'config' })!;
        expect(v.line).toBe('Memory limit per process: 32,768 MiB');
        expect(v.suffix).toBe('');
        expect(v.editable).toBe(true);
        expect(v.envNote).toBe('');
        expect(v.warning).toBe('');
    });
    it('labels 0 as no limit', () => {
        const v = memoryLimitView({ shell_memory_limit_mb: 0, shell_memory_limit_source: 'config' })!;
        expect(v.line).toBe('Memory limit per process: no limit');
        expect(v.warning).toBe('');
    });
    it('marks the default, and an unknown source reads as the default', () => {
        expect(memoryLimitView({ shell_memory_limit_mb: 8192, shell_memory_limit_source: 'default' })!.suffix).toBe('(default)');
        expect(memoryLimitView({ shell_memory_limit_mb: 8192 })!.suffix).toBe('(default)');
        expect(memoryLimitView({ shell_memory_limit_mb: 8192, shell_memory_limit_source: 'registry' })!.editable).toBe(true);
    });
    it('is read-only from the environment and names both variables', () => {
        const v = memoryLimitView({ shell_memory_limit_mb: 2000, shell_memory_limit_source: 'env' })!;
        expect(v.editable).toBe(false);
        expect(v.suffix).toBe('');
        for (const name of MEMORY_LIMIT_ENV_NAMES) expect(v.envNote).toContain(name);
        expect(v.envNote).toContain('in the environment');
    });
    it('carries the warning for a limit below 1,024 MiB', () => {
        const v = memoryLimitView({ shell_memory_limit_mb: 512, shell_memory_limit_source: 'config' })!;
        expect(v.warning).toBe(memoryLimitWarning(512));
        expect(v.warning).not.toBe('');
    });
});
