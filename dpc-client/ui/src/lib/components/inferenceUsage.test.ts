import { describe, it, expect } from 'vitest';
import {
  badgesOf,
  formatAmount,
  formatDuration,
  formatOwed,
  formatTokens,
  monthKey,
  monthLabel,
  nodeLabel,
  shapeUsage,
  shiftMonth,
  type NamedNode,
  type UsageResponse,
  type WireGroup,
} from './inferenceUsage';

const ALICE = 'dpc-node-' + 'a'.repeat(32);
const BOB = 'dpc-node-' + 'b'.repeat(32);

const NAMES: NamedNode[] = [
  { node_id: ALICE, label: 'Alice' },
];

/** One group as `_new_role_entry` writes it, with the overrides a case needs. */
const group = (over: Partial<WireGroup> = {}): WireGroup => ({
  row_count: 1,
  prompt_tokens: 1000,
  completion_tokens: 500,
  thinking_tokens: 0,
  duration_s: 4,
  counts_source: { ours: 0, engine: 1 },
  peer_proved: { true: 1, false: 0, none: 0 },
  cost_usd: 0,
  unpriced: 0,
  tariff: {},
  tariff_unpriceable: 0,
  untariffed: 0,
  ...over,
});

/** The four shapes a real partition holds, as the backend's own worked example
 *  folds them (tests/test_the_two_sides_of_a_shared_call_each_read_their_own_numbers.py). */
const response = (): UsageResponse => ({
  status: 'success',
  served: {
    by_caller: {
      [ALICE]: group({
        counts_source: { ours: 1, engine: 0 },
        tariff: { EUR: { amount: 0.007, rows: 1 } },
        by_alias: {
          ollama_local: group({
            counts_source: { ours: 1, engine: 0 },
            tariff: { EUR: { amount: 0.007, rows: 1 } },
          }),
        },
      }),
    },
    by_alias: {
      ollama_local: group({ tariff: { EUR: { amount: 0.007, rows: 1 } } }),
    },
  },
  consumed: {
    by_source: {
      [`remote:${BOB}:bob_glm`]: group({
        row_count: 2,
        node_id: BOB,
        alias: 'bob_glm',
        cost_usd: 0,
        unpriced: 2,
        tariff: { EUR: { amount: 0.0014, rows: 1 } },
        tariff_unpriceable: 1,
        peer_proved: { true: 2, false: 0, none: 0 },
      }),
    },
  },
  own: {
    by_alias: {
      ds_flash: group({
        prompt_tokens: 100,
        completion_tokens: 50,
        duration_s: 1,
        cost_usd: 0.02,
        untariffed: 1,
        peer_proved: { true: 0, false: 0, none: 1 },
      }),
    },
  },
});

describe('the three lists', () => {
  it('folds the response into served, consumed and own, in that order', () => {
    const view = shapeUsage(response(), NAMES, '2026-09');
    expect(view.lists.map((l) => l.role)).toEqual(['served', 'consumed', 'own']);
    expect(view.lists.map((l) => l.title)).toEqual(['Served to peers', 'Consumed', 'Own']);
    expect(view.lists.map((l) => l.rows.length)).toEqual([1, 1, 1]);
    expect(view.month).toBe('2026-09');
  });

  it('says to whom, how much and for what on a served row', () => {
    const [served] = shapeUsage(response(), NAMES, '2026-09').lists;
    const row = served.rows[0];
    expect(row.label).toBe('Alice');
    expect(row.id).toBe(ALICE);
    expect(row.calls).toBe(1);
    expect(row.promptTokens).toBe(1000);
    expect(row.completionTokens).toBe(500);
    expect(row.durationS).toBe(4);
    expect(row.owed).toEqual([{ currency: 'EUR', amount: 0.007, rows: 1 }]);
    expect(row.parts.map((p) => p.alias)).toEqual(['ollama_local']);
  });

  it('names the host of a consumed row and what is owed to it', () => {
    const [, consumed] = shapeUsage(response(), NAMES, '2026-09').lists;
    const row = consumed.rows[0];
    expect(row.id).toBe(BOB);
    expect(row.alias).toBe('bob_glm');
    // Bob has no name in the registry, so the row shows his id rather than nothing.
    expect(row.label).toBe(BOB);
    expect(row.owed).toEqual([{ currency: 'EUR', amount: 0.0014, rows: 1 }]);
    expect(row.unpriced).toBe(2);
  });

  it('shows an own row by the alias that ran and the dollars this node spent', () => {
    const [, , own] = shapeUsage(response(), NAMES, '2026-09').lists;
    expect(own.rows[0].label).toBe('ds_flash');
    expect(own.rows[0].alias).toBe('ds_flash');
    expect(own.rows[0].costUsd).toBe(0.02);
  });

  it('orders rows busiest first, then by label', () => {
    const many: UsageResponse = {
      own: {
        by_alias: {
          zed: group({ row_count: 5 }),
          amber: group({ row_count: 9 }),
          basil: group({ row_count: 5 }),
        },
      },
    };
    const [, , own] = shapeUsage(many, NAMES, '2026-09').lists;
    expect(own.rows.map((r) => r.label)).toEqual(['amber', 'basil', 'zed']);
  });
});

describe('an empty period', () => {
  it('gives every list its own empty line and no rows', () => {
    const view = shapeUsage({ status: 'success' }, NAMES, '2026-09');
    expect(view.lists.map((l) => l.rows.length)).toEqual([0, 0, 0]);
    expect(view.lists.map((l) => l.empty)).toEqual([
      'No peer has called this node yet.',
      "This node has called no peer's or vendor's model yet.",
      'No own usage this month.',
    ]);
    expect(view.hasUnpriceable).toBe(false);
  });

  it('shapes a null response into three empty lists rather than failing', () => {
    expect(shapeUsage(null, null, '2026-09').lists.map((l) => l.rows.length)).toEqual([0, 0, 0]);
  });
});

describe('the name a row carries', () => {
  it('uses the name the tab already knows', () => {
    expect(nodeLabel(ALICE, new Map([[ALICE, 'Alice']]))).toBe('Alice');
  });

  it('shows the bare id when no name is known, never a blank', () => {
    expect(nodeLabel(BOB, new Map())).toBe(BOB);
    const view = shapeUsage(response(), [], '2026-09');
    expect(view.lists[0].rows[0].label).toBe(ALICE);
  });

  it('says the host is not recorded when the consumed row named none', () => {
    const pending: UsageResponse = {
      consumed: { by_source: { bob_glm: group({ node_id: null, alias: 'bob_glm' }) } },
    };
    const row = shapeUsage(pending, NAMES, '2026-09').lists[1].rows[0];
    expect(row.id).toBeNull();
    expect(row.label).toBe('bob_glm');
    expect(row.badges[0]).toEqual({ kind: 'missing', text: 'host not recorded' });
  });
});

describe('the badges a group earns', () => {
  it('counts the rows this node recounted itself', () => {
    expect(badgesOf(group({ counts_source: { ours: 3, engine: 1 } }), 'served'))
      .toContainEqual({ kind: 'quota', text: '3 recounted' });
  });

  it('counts a tariff that applied over counts nobody could price', () => {
    expect(badgesOf(group({ tariff_unpriceable: 2 }), 'consumed'))
      .toContainEqual({ kind: 'missing', text: '2 unpriceable' });
  });

  it('calls a group a gift only when every row of it was untariffed', () => {
    expect(badgesOf(group({ row_count: 4, untariffed: 4 }), 'served'))
      .toContainEqual({ kind: 'gift', text: 'gift' });
    expect(badgesOf(group({ row_count: 4, untariffed: 3 }), 'served'))
      .not.toContainEqual({ kind: 'gift', text: 'gift' });
  });

  it('counts the calls whose far end was not proved, and those nobody asked', () => {
    const badges = badgesOf(group({ peer_proved: { true: 1, false: 2, none: 3 } }), 'consumed');
    expect(badges).toContainEqual({ kind: 'missing', text: '2 unproved' });
    expect(badges).toContainEqual({ kind: 'first', text: '3 unknown' });
  });

  it('asks nothing about proof on an own row, where there is no far end', () => {
    const badges = badgesOf(group({ peer_proved: { true: 0, false: 0, none: 7 } }), 'own');
    expect(badges.map((b) => b.text)).not.toContain('7 unknown');
  });

  it('earns no badge from a quiet group', () => {
    expect(badgesOf(group(), 'served')).toEqual([]);
  });
});

describe('unpriceable rows', () => {
  it('marks the list and the view when a group carries one', () => {
    const view = shapeUsage(response(), NAMES, '2026-09');
    expect(view.lists[1].hasUnpriceable).toBe(true);
    expect(view.lists[0].hasUnpriceable).toBe(false);
    expect(view.hasUnpriceable).toBe(true);
  });
});

describe('what is owed', () => {
  it('writes one string per currency and never adds two together', () => {
    expect(formatOwed([
      { currency: 'USD', amount: 1.5, rows: 2 },
      { currency: 'EUR', amount: 0.007, rows: 1 },
    ])).toBe('1.50 USD + 0.0070 EUR');
  });

  it('says nothing owed with a dash rather than a zero', () => {
    expect(formatOwed([])).toBe('—');
  });

  it('keeps a charge too small for cents visible', () => {
    expect(formatAmount(0.007)).toBe('0.0070');
    expect(formatAmount(0.0000012)).toBe('1.2e-6');
    expect(formatAmount(0)).toBe('0.00');
    expect(formatAmount(12.5)).toBe('12.50');
  });

  it('sorts the currencies so two runs read alike', () => {
    const row = shapeUsage({
      own: { by_alias: { a: group({ tariff: { USD: { amount: 1, rows: 1 }, EUR: { amount: 2, rows: 1 } } }) } },
    }, NAMES, '2026-09').lists[2].rows[0];
    expect(row.owed.map((o) => o.currency)).toEqual(['EUR', 'USD']);
  });
});

describe('the month, which is the period', () => {
  it('is the UTC month of the moment asked about', () => {
    expect(monthKey(new Date('2026-09-14T12:00:00Z'))).toBe('2026-09');
    expect(monthKey(new Date('2026-01-01T00:30:00Z'))).toBe('2026-01');
  });

  it('steps back and forward across a year boundary', () => {
    expect(shiftMonth('2026-01', -1)).toBe('2025-12');
    expect(shiftMonth('2025-12', 1)).toBe('2026-01');
    expect(shiftMonth('2026-09', -9)).toBe('2025-12');
    expect(shiftMonth('2026-09', 4)).toBe('2027-01');
  });

  it('leaves a key it cannot read alone', () => {
    expect(shiftMonth('not-a-month', 1)).toBe('not-a-month');
    expect(monthLabel('not-a-month')).toBe('not-a-month');
  });

  it('reads as a month and a year', () => {
    expect(monthLabel('2026-09')).toBe('September 2026');
  });
});

describe('the numbers as they are shown', () => {
  it('groups thousands the same way on every machine', () => {
    expect(formatTokens(1000)).toBe('1 000');
    expect(formatTokens(1234567)).toBe('1 234 567');
    expect(formatTokens(0)).toBe('0');
  });

  it('writes a duration in the largest unit it fills', () => {
    expect(formatDuration(4)).toBe('4.0 s');
    expect(formatDuration(95)).toBe('1 m 35 s');
    expect(formatDuration(3700)).toBe('1 h 1 m');
  });
});
