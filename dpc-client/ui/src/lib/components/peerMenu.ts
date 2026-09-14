// peerMenu.ts — the pure half of what a guest reads about a peer's alias before
// it calls it.
//
// The menu is `GET_PROVIDERS` → `PROVIDERS_RESPONSE` (DPTP §3.5), and since
// `ec686608` each row can carry `tariff` — what this guest is charged, resolved
// by the host the same way it prices the call — and `settings`, the dials the
// call will run at. Mike's rule, 2026-09-14: a guest sees the host's full
// effective settings and the price before choosing, and what it gets is the
// host's settings; the one dial it owns is reasoning effort, under the host's
// cap.
//
// Both fields are fail-closed on the wire and are read fail-closed here:
//
//   * no `tariff` key at all is a **gift** — the host declared no price. It is
//     not «free». The glossary keeps the two apart (`tariff`: null = a gift,
//     0 = declared free), and printing «free» for a gift would tell a guest a
//     host decided something it never decided.
//   * an absent setting is «not stated by the host», never «default» or
//     «none»: a vendor default the host never chose still applies at the vendor.
//   * a `unit` this build does not know is not scaled, not converted and not
//     compared — the raw numbers are shown as text and nothing is computed
//     from them (§3.5: «a receiver that does not know the word must not price
//     the row»).
//
// The three states carry the host-side words `gift` / `free` / `paid` that
// `inferenceSharing.ts:tariffState` already uses for the same three states seen
// from the owner's side. No DOM here; this is what the tests exercise.

/** The only unit §3.5 defines for `tariff.in` / `tariff.out`. */
export const PER_1M_TOKENS = 'per_1m_tokens';

/** What a guest reads where the host stated nothing. */
export const NOT_STATED = 'not stated by the host';

/** `tariff` on a menu row, as §3.5 writes it. Every field is required on the
 *  wire and optional here: a row that lost one is read fail-closed, not
 *  guessed. */
export interface MenuTariff {
  in?: number | null;
  out?: number | null;
  currency?: string | null;
  from?: string | null;
  unit?: string | null;
  free?: boolean | null;
}

/** `settings` on a menu row (§3.5). Absent keys are the host's silence. */
export interface MenuSettings {
  temperature?: number | null;
  top_p?: number | null;
  top_k?: number | null;
  max_output_tokens?: number | null;
  variant?: string | null;
}

/** One row of `PROVIDERS_RESPONSE.providers`, local or from a peer. */
export interface MenuRow {
  alias: string;
  model?: string;
  type?: string;
  supports_vision?: boolean;
  supports_voice?: boolean;
  supports_tools?: boolean;
  context_window?: number | null;
  reasoning_words?: string[] | null;
  reasoning_default?: string | null;
  tariff?: MenuTariff | null;
  settings?: MenuSettings | null;
}

/**
 * `gift` — no tariff declared; `free` — declared zero; `paid` — a rate;
 * `unpriceable` — a tariff arrived that this build must not price (a unit it
 * does not know, or rates it cannot read).
 */
export type PriceKind = 'gift' | 'free' | 'paid' | 'unpriceable';

export interface PriceLine {
  kind: PriceKind;
  /** The full line, for the expanded panel. */
  text: string;
  /** The same fact in as few words as the summary line can hold. */
  short: string;
}

const GIFT: PriceLine = {
  kind: 'gift',
  text: 'no price declared (a gift)',
  short: 'a gift',
};

const FREE: PriceLine = {
  kind: 'free',
  text: 'free for you',
  short: 'free for you',
};

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/**
 * How many fraction digits show every rate in the pair without rounding one of
 * them away. A rate of 0.07 that printed as «0» would be a wrong price, not a
 * short one; one count is used for both numbers so the pair reads as a pair.
 */
function fractionDigits(rates: readonly number[]): number {
  let digits = 2;
  for (const rate of rates) {
    const magnitude = Math.abs(rate);
    if (magnitude === 0) continue;
    digits = Math.max(digits, Math.min(8, Math.ceil(-Math.log10(magnitude)) + 1));
  }
  return digits;
}

/**
 * One rate in the host's currency. `Intl` is asked for the code and prints
 * whatever symbol the reader's own locale knows for it; a code it refuses —
 * anything that is not three letters — falls back to the bare code beside the
 * number, because inventing a symbol for a currency we do not know is how a
 * price becomes a different price.
 *
 * `locale` is a parameter only so the tests can pin one; the application passes
 * none and gets the reader's.
 */
export function formatRate(
  rate: number,
  currency: string | null | undefined,
  digits: number,
  locale?: string,
): string {
  const options: Intl.NumberFormatOptions = {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  };
  if (typeof currency === 'string' && currency.length > 0) {
    try {
      return new Intl.NumberFormat(locale, {
        ...options,
        style: 'currency',
        currency,
      }).format(rate);
    } catch {
      return `${new Intl.NumberFormat(locale, options).format(rate)} ${currency}`;
    }
  }
  return new Intl.NumberFormat(locale, options).format(rate);
}

/**
 * What this guest is charged for a call on the alias this row describes.
 *
 * Order of the reading, and why: `free` is answered before the unit, because
 * zero is zero under any unit and a host that said «free» said it about this
 * guest; the unit is answered before the arithmetic, because a scale this build
 * does not know is one it must not apply.
 */
export function priceLine(
  tariff: MenuTariff | null | undefined,
  locale?: string,
): PriceLine {
  if (tariff === null || tariff === undefined || typeof tariff !== 'object') {
    return GIFT;
  }

  const rin = tariff.in;
  const rout = tariff.out;
  const from = typeof tariff.from === 'string' && tariff.from.length > 0 ? tariff.from : null;
  const since = from ? `, from ${from}` : '';

  if (tariff.free === true) return FREE;
  if (isFiniteNumber(rin) && isFiniteNumber(rout) && rin === 0 && rout === 0) return FREE;

  if (!isFiniteNumber(rin) || !isFiniteNumber(rout)) {
    return {
      kind: 'unpriceable',
      text: 'a tariff arrived without readable rates — nothing is priced from it',
      short: 'rates not readable',
    };
  }

  const currency = typeof tariff.currency === 'string' && tariff.currency.length > 0
    ? tariff.currency
    : null;

  if (tariff.unit !== PER_1M_TOKENS) {
    // Raw numbers, as text. Nothing is scaled, formatted as money or summed:
    // the unit says what these numbers mean and we do not know the word.
    const named = typeof tariff.unit === 'string' && tariff.unit.length > 0
      ? `unit "${tariff.unit}" is not one this build knows`
      : 'the host stated no unit';
    const money = currency ? ` ${currency}` : '';
    return {
      kind: 'unpriceable',
      text: `${rin} / ${rout}${money} in / out — ${named}, so nothing is priced from it${since}`,
      short: 'unit not known',
    };
  }

  const digits = fractionDigits([rin, rout]);
  const inText = formatRate(rin, currency, digits, locale);
  const outText = formatRate(rout, currency, digits, locale);
  return {
    kind: 'paid',
    text: `${inText} / ${outText} per 1M tokens in / out${since}`,
    short: `${inText} / ${outText} per 1M`,
  };
}

/** One dial, as the panel prints it. `key` is the wire's own word (§3.5). */
export interface SettingLine {
  key: string;
  value: string;
  /** False where the host stated nothing, which is what greys the row. */
  stated: boolean;
}

function stateOf(value: unknown): SettingLine['value'] | null {
  if (isFiniteNumber(value)) return String(value);
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : null;
  }
  return null;
}

function line(key: string, value: unknown): SettingLine {
  const stated = stateOf(value);
  return stated === null
    ? { key, value: NOT_STATED, stated: false }
    : { key, value: stated, stated: true };
}

/**
 * The block titled «Runs at the host's settings»: the five dials §3.5 puts in
 * `settings`, then the two that live at the top of the row and are not repeated
 * inside it. Every key is always present as a line — a dial the host did not
 * state is a fact worth reading, and dropping the row would read as «there is
 * no such dial».
 */
export function settingsLines(row: MenuRow | null | undefined): SettingLine[] {
  const settings = (row && typeof row.settings === 'object' && row.settings !== null)
    ? row.settings
    : {};
  return [
    line('temperature', settings.temperature),
    line('top_p', settings.top_p),
    line('top_k', settings.top_k),
    line('max_output_tokens', settings.max_output_tokens),
    line('variant', settings.variant),
    line('context_window', row?.context_window),
    line('reasoning_default', row?.reasoning_default),
  ];
}

/**
 * The one line the panel shows collapsed:
 * `<alias> · <price> · ctx <n> · effort <default>`.
 */
export function menuSummary(row: MenuRow | null | undefined, locale?: string): string {
  const alias = (row?.alias ?? '').trim() || 'this alias';
  const price = priceLine(row?.tariff, locale).short;
  const ctx = isFiniteNumber(row?.context_window)
    ? `ctx ${row?.context_window}`
    : 'ctx not stated';
  const effort = stateOf(row?.reasoning_default) !== null
    ? `effort ${String(row?.reasoning_default).trim()}`
    : 'effort not stated';
  return `${alias} · ${price} · ${ctx} · ${effort}`;
}
