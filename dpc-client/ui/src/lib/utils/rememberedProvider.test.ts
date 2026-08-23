/**
 * «при переключении между чатами и возврате в Local AI chat там всегда
 * используется в Text та модель что по дефолту установлена, а не та которую
 * выбирал пользователь в этом чате» — Mike, 2026-08-16.
 *
 * Reproduced from the log rather than from a repro request: three queries on
 * `muse-glimmer:latest` (local, free), a detour into another chat, and the very
 * next query back in Local AI Chat routed to `deepseek_flash` — `default_provider`
 * — at `effort=max`, 487 reasoning tokens for «какое сегодня число?». The silent
 * switch has a direction, and it is towards money.
 */

import { describe, it, expect } from 'vitest';
import { providerToRemember } from './rememberedProvider';

describe('a local choice', () => {
    it('is remembered as the bare alias the payload expects', () => {
        expect(providerToRemember('local:muse-glimmer:latest', undefined))
            .toBe('muse-glimmer:latest');
    });

    it('including an alias that contains a colon of its own', () => {
        // Ollama tags are `name:tag`, so stripping to the last colon would eat
        // half the alias — only the prefix comes off.
        expect(providerToRemember('local:qwen3.8:27b', null)).toBe('qwen3.8:27b');
    });
});

describe('a remote choice', () => {
    it('is not remembered, because the map holds aliases and this is a host', () => {
        // `remote:…` in that map would reach the backend as `provider`, where an
        // alias is expected — a wrong value stored is worse than none.
        expect(providerToRemember('remote:dpc-node-abc:llama', undefined)).toBeNull();
    });
});

describe('nothing to write', () => {
    it('when the value has not changed', () => {
        expect(providerToRemember('local:deepseek_flash', 'deepseek_flash')).toBeNull();
    });

    it('when there is no alias after the prefix', () => {
        expect(providerToRemember('local:', undefined)).toBeNull();
    });

    it('when the dropdown gave nothing at all', () => {
        expect(providerToRemember('', undefined)).toBeNull();
        expect(providerToRemember(null, undefined)).toBeNull();
        expect(providerToRemember(undefined, undefined)).toBeNull();
    });
});
