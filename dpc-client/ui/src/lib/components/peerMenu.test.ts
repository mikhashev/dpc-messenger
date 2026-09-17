/**
 * What a guest reads about a peer's alias before it calls it.
 *
 * The board entry is A-PEER-LEARNS-THE-TARIFF-BEFORE-IT-CALLS: until `ec686608`
 * a guest could learn a price only from the answer that had already cost it,
 * and the temperature, the output ceiling and the quantisation not at all. The
 * backend half put `tariff` and `settings` on the menu row (DPTP §3.5); this is
 * the guest-side reading of them, and the states it must never confuse are the
 * ones the glossary keeps apart: a **gift** (no tariff declared) is not
 * **free** (a declared zero), and a setting the host did not state is not a
 * setting that does not apply.
 */

import { describe, it, expect } from 'vitest';
import {
  NOT_STATED,
  menuSummary,
  priceLine,
  settingsLines,
  type MenuRow,
} from './peerMenu';

const paid = {
  in: 20,
  out: 60,
  currency: 'RUB',
  from: '2026-09-01',
  unit: 'per_1m_tokens',
  free: false,
};

describe('a tariff nobody declared', () => {
  it('is a gift, and the word free does not appear', () => {
    const line = priceLine(undefined);
    expect(line.kind).toBe('gift');
    expect(line.text).toBe('no price declared (a gift)');
    expect(line.text).not.toMatch(/free/i);
    // Nor a zero, which would be a price somebody chose.
    expect(line.text).not.toMatch(/0/);
  });

  it('the same for an explicit null, which is how an absent key arrives', () => {
    expect(priceLine(null).kind).toBe('gift');
  });
});

describe('a tariff declared free', () => {
  it('says so for this guest, because free is about the recipient', () => {
    const line = priceLine({ ...paid, in: 0, out: 0, free: true });
    expect(line.kind).toBe('free');
    expect(line.text).toBe('free for you');
  });

  it('is read from zero rates too, since a declared zero is a price of zero', () => {
    // §3.5 says zeros and `free: false` cannot occur together; a row that
    // carries both is malformed, and the rates are the load-bearing half.
    expect(priceLine({ ...paid, in: 0, out: 0, free: false }).kind).toBe('free');
  });

  it('and free wins over a unit we could not have priced anyway', () => {
    // Zero is zero under any scale, so an unknown unit does not make the
    // host's «free for you» unreadable.
    expect(priceLine({ in: 0, out: 0, currency: 'RUB', unit: 'per_word', free: true }).kind)
      .toBe('free');
  });
});

describe('a tariff with a rate', () => {
  it('quotes both rates per 1M tokens and the day the line began', () => {
    const line = priceLine({ ...paid, currency: 'USD' }, 'en-US');
    expect(line.kind).toBe('paid');
    expect(line.text).toBe('$20 / $60 per 1M tokens in / out, from 2026-09-01');
  });

  it('prints the symbol the reader s own locale knows for the code', () => {
    const line = priceLine(paid, 'ru-RU');
    expect(line.kind).toBe('paid');
    expect(line.text).toContain('₽');
    expect(line.text).toContain('20');
    expect(line.text).toContain('60');
    expect(line.text).toContain('from 2026-09-01');
  });

  it('falls back to the bare code where Intl refuses the currency', () => {
    // A symbol invented for a currency we do not know would be a different
    // price; the code itself never is.
    const line = priceLine({ ...paid, currency: 'RUBLE' }, 'en-US');
    expect(line.kind).toBe('paid');
    expect(line.text).toBe('20 RUBLE / 60 RUBLE per 1M tokens in / out, from 2026-09-01');
  });

  it('keeps a fraction of a unit from rounding away to zero', () => {
    // Two decimals would print the cheaper rate as «$0», which is not a
    // shorter price but a different one.
    const line = priceLine({ ...paid, in: 0.004, out: 0.07, currency: 'USD' }, 'en-US');
    expect(line.text).toBe('$0.004 / $0.07 per 1M tokens in / out, from 2026-09-01');
  });

  it('leaves the date out rather than inventing one', () => {
    const line = priceLine({ ...paid, currency: 'USD', from: null }, 'en-US');
    expect(line.text).toBe('$20 / $60 per 1M tokens in / out');
  });
});

describe('a unit this build does not know', () => {
  it('shows the raw numbers and prices nothing from them', () => {
    const line = priceLine({ ...paid, unit: 'per_1k_tokens' }, 'en-US');
    expect(line.kind).toBe('unpriceable');
    expect(line.text).toContain('per_1k_tokens');
    expect(line.text).toContain('20 / 60 RUB');
    expect(line.text).toContain('nothing is priced');
    // Not scaled, and not dressed as money either.
    expect(line.text).not.toContain('per 1M');
    expect(line.text).not.toContain('₽');
    expect(line.short).toBe('unit not known');
  });

  it('including no unit at all, which §3.5 requires and this row lost', () => {
    const line = priceLine({ ...paid, unit: null }, 'en-US');
    expect(line.kind).toBe('unpriceable');
    expect(line.text).toContain('the host stated no unit');
  });

  it('and rates that are not numbers price nothing either', () => {
    const line = priceLine({ ...paid, in: null }, 'en-US');
    expect(line.kind).toBe('unpriceable');
    expect(line.short).toBe('rates not readable');
    expect(line.text).not.toMatch(/free|gift/i);
  });
});

describe('the settings block', () => {
  const row: MenuRow = {
    alias: 'gpt-oss-120b',
    model: 'gpt-oss-120b',
    context_window: 131072,
    reasoning_default: 'xhigh',
    settings: {
      temperature: 1.0,
      top_p: 0.95,
      top_k: 20,
      max_output_tokens: 8192,
      variant: 'gpt-oss-120b-Q5_K_M.gguf',
    },
  };

  it('names the five dials of §3.5 and the two that sit above them', () => {
    expect(settingsLines(row).map((l) => l.key)).toEqual([
      'temperature',
      'top_p',
      'top_k',
      'max_output_tokens',
      'variant',
      'context_window',
      'reasoning_default',
    ]);
  });

  it('carries each value the host stated', () => {
    const by = new Map(settingsLines(row).map((l) => [l.key, l]));
    expect(by.get('temperature')?.value).toBe('1');
    expect(by.get('top_p')?.value).toBe('0.95');
    expect(by.get('top_k')?.value).toBe('20');
    expect(by.get('max_output_tokens')?.value).toBe('8192');
    expect(by.get('variant')?.value).toBe('gpt-oss-120b-Q5_K_M.gguf');
    expect(by.get('context_window')?.value).toBe('131072');
    expect(by.get('reasoning_default')?.value).toBe('xhigh');
    expect(settingsLines(row).every((l) => l.stated)).toBe(true);
  });

  it('reads an absent key as the host s silence, never as a default or a none', () => {
    // A vendor default the host never chose still applies at the vendor, so
    // «default» here would be this build speaking for a host that did not.
    const lines = settingsLines({ alias: 'a', settings: { temperature: 0.2 } });
    const by = new Map(lines.map((l) => [l.key, l]));
    expect(by.get('temperature')?.value).toBe('0.2');
    expect(by.get('top_p')?.value).toBe(NOT_STATED);
    expect(by.get('top_p')?.stated).toBe(false);
    for (const l of lines) {
      expect(l.value).not.toMatch(/\b(default|none)\b/i);
    }
  });

  it('and a row with no settings object at all still shows every line', () => {
    const lines = settingsLines({ alias: 'a' });
    expect(lines).toHaveLength(7);
    expect(lines.every((l) => l.value === NOT_STATED && !l.stated)).toBe(true);
  });

  it('a zero temperature is a stated temperature, not a missing one', () => {
    const by = new Map(settingsLines({ alias: 'a', settings: { temperature: 0 } })
      .map((l) => [l.key, l]));
    expect(by.get('temperature')?.value).toBe('0');
    expect(by.get('temperature')?.stated).toBe(true);
  });
});

describe('the one line the panel shows collapsed', () => {
  it('carries the alias, the price, the window and the effort', () => {
    const row: MenuRow = {
      alias: 'gpt-oss-120b',
      context_window: 131072,
      reasoning_default: 'xhigh',
      tariff: { ...paid, currency: 'USD' },
    };
    expect(menuSummary(row, 'en-US'))
      .toBe('gpt-oss-120b · $20 / $60 per 1M · ctx 131072 · effort xhigh');
  });

  it('says gift where no tariff was declared', () => {
    const row: MenuRow = { alias: 'llama', context_window: 8192, reasoning_default: 'low' };
    expect(menuSummary(row, 'en-US')).toBe('llama · a gift · ctx 8192 · effort low');
  });

  it('says what is not stated rather than filling it in', () => {
    const row: MenuRow = { alias: 'llama', tariff: { ...paid, free: true } };
    expect(menuSummary(row, 'en-US'))
      .toBe('llama · free for you · ctx not stated · effort not stated');
  });
});
