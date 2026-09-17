// src/lib/utils/retryCountdown.ts
// How much of an announced backoff is left.
//
// The backend announces each wait once, with its length; the strip has to make
// that look like time passing rather than a frozen number. Deriving the figure
// from a clock keeps it honest across a tab that was backgrounded — a decrement
// per tick would drift by however long the timer was throttled.
//
// It lives here rather than in the component so it can be tested, which is the
// same reason contextOccupancy.ts exists.

/** Whole seconds left of `waitingSeconds`, counted from `arrivedAtMs` to `nowMs`. */
export function secondsLeft(waitingSeconds: number, arrivedAtMs: number, nowMs: number): number {
    const elapsed = (nowMs - arrivedAtMs) / 1000;
    // Ceil so a wait of 3s reads "3s" for its first second rather than "2s":
    // the number should name the wait the person is being asked to sit through.
    return Math.max(0, Math.ceil(waitingSeconds - elapsed));
}
