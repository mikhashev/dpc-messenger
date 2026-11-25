// The GPU card's queue for the Agent map (ADR-052; Mike, 2026-11-25): who holds the card since
// when, who waits in order with the lease's own estimate, and what just happened. The snapshot is
// the backend's `card_lease.queue_snapshot()`, read with `get_card_lease_status` and pushed as
// `card_lease_updated`; every word on the screen is built here.

import { formatWait, ordinal } from './agentTurnNotice';

export interface CardHolder {
    /** The holder key (`whisper:turbo`, `music:yue2`, a llama-server alias). */
    client: string;
    /** For people: `Whisper (turbo)`. */
    display: string;
    /** A lease's kind (`transcription`, `music`, ...) or `inference` for a supervised engine. */
    kind: string;
    engine: string;
    state: string;
    busy: boolean;
    purpose: string | null;
    agent_id: string | null;
    conversation_id: string | null;
    /** Epoch seconds the grant was made; null for a supervised engine (it keeps no such time). */
    since: number | null;
    eta_s: number | null;
    beside: string | null;
}

export interface CardWaiter {
    position: number;
    client: string;
    display: string;
    kind: string;
    purpose: string | null;
    agent_id: string | null;
    conversation_id: string | null;
    /** Epoch seconds the request took its place. */
    since: number;
    give_up_at: number | null;
    until_cancelled: boolean;
    /** The lease's own estimate; unknown is `wait_known: false` and `wait_s: null`, never 0. */
    wait_s: number | null;
    wait_known: boolean;
    wait_upper_bound: boolean;
    waits_for: string[];
    where: string;
    for_a_peer: boolean;
}

export interface CardRecent {
    at: number;
    event: 'granted' | 'released' | 'left_the_line' | string;
    client: string;
    display: string;
    purpose: string | null;
    why?: string;
    held_s?: number;
    waited_s?: number;
}

export interface CardLeaseSnapshot {
    schema: string;
    updated_at: number;
    backend_pid: number;
    free: boolean;
    closed: string | null;
    holder: CardHolder | null;
    holders: CardHolder[];
    waiters: CardWaiter[];
    recent: CardRecent[];
}

/** Whether `next` is a snapshot to show: the newest by its own clock wins, so a read that lands after
 *  a later push does not put the older queue back. */
export function newerSnapshot(current: CardLeaseSnapshot | null, next: unknown): CardLeaseSnapshot | null {
    const snap = next as CardLeaseSnapshot | null;
    if (!snap || typeof snap !== 'object' || !Array.isArray(snap.holders) || !Array.isArray(snap.waiters)) {
        return current;
    }
    if (current && typeof current.updated_at === 'number' && snap.updated_at < current.updated_at) return current;
    return snap;
}

const pad = (n: number) => String(n).padStart(2, '0');

/** Local wall-clock time of epoch seconds: `14:02`. */
export function clockOfSeconds(seconds: number | null | undefined): string {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '—';
    const d = new Date(seconds * 1000);
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** A wait the lease estimated: `about 20 s`, `at most 2 min 0 s` (a declared bound), else `unknown`. */
export function estimateWords(w: Pick<CardWaiter, 'wait_s' | 'wait_known' | 'wait_upper_bound'>): string {
    if (!w.wait_known || typeof w.wait_s !== 'number' || !Number.isFinite(w.wait_s)) return 'unknown';
    return `${w.wait_upper_bound ? 'at most' : 'about'} ${formatWait(w.wait_s)}`;
}

export interface CardHolderLine {
    key: string;
    name: string;
    /** What it is doing, with the agent it runs for: `dictation · for Ark`, or `idle`. */
    purpose: string;
    /** `since 14:02 · 1 min 8 s`; empty when the holder keeps no grant time. */
    since: string;
    /** `running`, `starting`, `draining`...; `idle` / `busy` follows from the holder. */
    state: string;
    busy: boolean;
}

export interface CardWaiterLine {
    key: string;
    position: number;
    name: string;
    purpose: string;
    waited: string;
    estimate: string;
    waitsFor: string;
    note: string;
}

export interface CardRecentLine {
    key: string;
    at: string;
    text: string;
}

export interface CardView {
    free: boolean;
    /** `Free` or `Held by Whisper (turbo)`, plus `· 2 waiting`. */
    headline: string;
    holders: CardHolderLine[];
    waiters: CardWaiterLine[];
    recent: CardRecentLine[];
    /** The backend is stopping and hands the card out no more. */
    closed: string;
}

type AgentName = (agentId: string) => string;

function forAgent(row: { agent_id: string | null }, agentName: AgentName): string {
    return row.agent_id ? `for ${agentName(row.agent_id)}` : '';
}

const join = (...parts: Array<string | null | undefined>) => parts.filter(Boolean).join(' · ');

/** The block's lines from a snapshot. `agentName` turns an agent id into the name the map shows. */
export function cardLeaseView(snap: CardLeaseSnapshot | null, nowMs: number,
                              agentName: AgentName = (id) => id): CardView | null {
    if (!snap) return null;
    const secondsSince = (at: number) => (nowMs - at * 1000) / 1000;
    const holders = snap.holders.map((h): CardHolderLine => ({
        key: h.client,
        name: h.display || h.client,
        purpose: join(h.purpose || (h.busy ? 'busy' : 'idle'), forAgent(h, agentName),
                      h.beside ? `beside ${h.beside}` : ''),
        since: typeof h.since === 'number' ? join(`since ${clockOfSeconds(h.since)}`, formatWait(secondsSince(h.since))) : '',
        state: h.state,
        busy: h.busy,
    }));
    const waiters = snap.waiters.map((w, i): CardWaiterLine => ({
        key: `${w.client}#${i}`,
        position: w.position,
        name: w.display || w.client,
        purpose: join(w.purpose || '', forAgent(w, agentName)),
        waited: `waiting ${formatWait(secondsSince(w.since))}`,
        estimate: estimateWords(w),
        waitsFor: w.waits_for.length ? `waits for ${w.waits_for.join(', ')}` : '',
        note: join(w.until_cancelled ? 'until cancelled' : '', w.for_a_peer ? "a peer's request" : ''),
    }));
    const recent = snap.recent.slice().reverse().map((r, i): CardRecentLine => {
        const verb = r.event === 'granted' ? 'got the card' : r.event === 'released' ? 'let go'
            : r.event === 'left_the_line' ? 'left the line' : r.event;
        const extra = join(
            typeof r.waited_s === 'number' ? `waited ${formatWait(r.waited_s)}` : '',
            typeof r.held_s === 'number' ? `held ${formatWait(r.held_s)}` : '',
            r.why ?? '',
        );
        return { key: `${r.at}#${i}`, at: clockOfSeconds(r.at),
                 text: `${r.display || r.client}${r.purpose ? ` (${r.purpose})` : ''} ${verb}${extra ? ` · ${extra}` : ''}` };
    });
    const headline = join(
        snap.free ? 'Free' : `Held by ${holders.map((h) => h.name).join(' + ')}`,
        snap.waiters.length ? `${snap.waiters.length} waiting` : '',
    );
    return { free: snap.free, headline, holders, waiters, recent, closed: snap.closed ?? '' };
}

/** What an agent's NOW line adds when its turn waits for the card: its place in the line and what it
 *  stands behind; empty when the snapshot has no request of that agent (an older backend, or the
 *  wait ended since the turn's last update). */
export function cardWaitNote(snap: CardLeaseSnapshot | null | undefined, agentId: string): string {
    const mine = snap?.waiters.find((w) => w.agent_id === agentId);
    if (!snap || !mine) return '';
    const behind = mine.waits_for[0] ?? snap.holders[0]?.display ?? '';
    return `${ordinal(mine.position)} in line for the card${behind ? `, behind ${behind}` : ''}`;
}
