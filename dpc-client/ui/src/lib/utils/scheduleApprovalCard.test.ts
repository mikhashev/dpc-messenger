/**
 * A card must not outlive the gate it belongs to.
 *
 * The backend stops waiting after its TTL and answers nobody afterwards; until
 * 2026-08-29 nothing took the card off the screen, so a later press reached a
 * request id the backend no longer held — and the front end dropped the card
 * before reading the refusal, which is why it looked as if nothing happened.
 * Mike raised it on 2026-08-16 and hit the same card twelve days later.
 */
import { describe, it, expect } from 'vitest';
import { cardLifetimeMs, scheduleCardRetirement } from './scheduleApprovalCard';

/** Records timers instead of running them, so the expiry needs no minute. */
function collector() {
  const fired: Array<{ fn: () => void; ms: number }> = [];
  return { fired, schedule: (fn: () => void, ms: number) => { fired.push({ fn, ms }); return 0; } };
}

describe('cardLifetimeMs', () => {
  it('is the deadline the backend named', () => {
    expect(cardLifetimeMs({ request_id: 'r', timeout_seconds: 60 })).toBe(60_000);
  });

  it('is nothing when the backend named none', () => {
    // A backend older than the change sends no field; the card then behaves as
    // it always did rather than vanishing on a number nobody sent.
    expect(cardLifetimeMs({ request_id: 'r' })).toBeNull();
    expect(cardLifetimeMs({ request_id: 'r', timeout_seconds: null })).toBeNull();
    expect(cardLifetimeMs({ request_id: 'r', timeout_seconds: 0 })).toBeNull();
    expect(cardLifetimeMs(null)).toBeNull();
  });

  it('refuses a deadline that is not a number', () => {
    expect(cardLifetimeMs({ request_id: 'r', timeout_seconds: NaN })).toBeNull();
    expect(cardLifetimeMs({ request_id: 'r', timeout_seconds: -5 })).toBeNull();
  });
});

describe('scheduleCardRetirement', () => {
  it('retires the card on the deadline, and names the one to retire', () => {
    const timers = collector();
    const retired: string[] = [];

    const armed = scheduleCardRetirement(
      { request_id: 'r-1', timeout_seconds: 60 },
      (id) => retired.push(id),
      timers.schedule,
    );

    expect(armed).toBe(true);
    expect(timers.fired[0].ms).toBe(60_000);
    expect(retired).toEqual([]);       // not before the deadline
    timers.fired[0].fn();
    expect(retired).toEqual(['r-1']);  // and exactly once it passes
  });

  it('arms nothing when there is no deadline, and says so', () => {
    const timers = collector();
    const retired: string[] = [];

    const armed = scheduleCardRetirement(
      { request_id: 'r-1' },
      (id) => retired.push(id),
      timers.schedule,
    );

    expect(armed).toBe(false);
    expect(timers.fired).toEqual([]);
    expect(retired).toEqual([]);
  });
});
