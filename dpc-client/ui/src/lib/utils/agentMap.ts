// The Agent map (ADR-046 point 8): for each agent its turn, the chats waiting
// for it and the processes it started; beside them the backend's own system
// branch. Built from the `list_processes` answer and the agentTurns store.
//
// Pure: the component fetches, keeps the clock and the selection; everything
// it prints is decided here. The turn's words come from agentTurnNotice.ts, so
// the map and the line in the waiting chat say one state one way.

import {
    formatWait,
    heldByApprovalSuffix,
    ordinal,
    silencePhrase,
    since,
    turnIsUnconfirmed,
    turnWaitPhrase,
    waitingReasonPhrase,
    type AgentTurnState,
    type AgentTurnStates,
} from './agentTurnNotice';
import type { FreeCommitReading } from './strataFields';

/** How often the open map reads `list_processes` again; processes have no push event. */
export const PROCESS_POLL_MS = 5000;
/** A finished process stays under its agent for this long after it ended. */
export const AGENT_PROCESS_WINDOW_S = 3600;
/** The system branch opens collapsed. */
export const SYSTEM_OPEN_BY_DEFAULT = false;

/** One entry of `list_processes` (process_registry.ProcessEntry plus `commit_mb`). */
export interface ProcessEntry {
    pid: number;
    start_time: number | null;
    parent_pid: number | null;
    parent_start_time: number | null;
    launcher: string;
    launcher_kind: 'tool' | 'subsystem';
    agent_id: string | null;
    conversation_id: string | null;
    tool: string | null;
    round: number | null;
    command: string;
    started_at: string | null;
    ended_at: string | null;
    exit_reason: string | null;
    stop_kind: string | null;
    exit_code: number | null;
    signal: string | null;
    peak_rss_mb: number | null;
    peak_commit_mb: number | null;
    peak_vram_mib: number | null;
    commit_mb: number | null;
}

/** The `list_processes` answer, without its status. */
export interface ProcessListing {
    processes: ProcessEntry[];
    backend_pid: number | null;
    backend_start_time: number | null;
    free_commit: FreeCommitReading | null;
}

/** working (green), waiting for the model or an approval (amber), idle or ended (grey). */
export type MapDot = 'working' | 'waiting' | 'idle';

export interface MapWaitingChat {
    conversation_id: string;
    chat: string;
    place: string;
    reason: string;
    wait: string;
}

export interface MapProcess {
    pid: number;
    command: string;
    chat: string;
    started: string;
    duration: string;
    rss: string;
    commit: string;
    end: string;
    alive: boolean;
    /** `warn` for an end other than exiting on its own. */
    endTone: 'alive' | 'ended' | 'warn';
}

export interface MapAgent {
    agent_id: string;
    name: string;
    model: string;
    effort: string;
    dot: MapDot;
    stateLine: string;
    nowLine: string;
    nowDetail: string;
    waiting: MapWaitingChat[];
    processes: MapProcess[];
    alive: number;
}

export interface MapSystemItem {
    name: string;
    line: string;
    alive: boolean;
}

export interface MapMemoryBar {
    kind: 'commit' | 'physical';
    label: string;
    fraction: number;
}

export interface AgentMap {
    agents: MapAgent[];
    system: MapSystemItem[];
    systemSummary: string;
    backendLine: string;
    summaryLine: string;
    memoryBars: MapMemoryBar[];
    memorySource: string;
}

/** The row fields of `list_agents` the map reads. */
export interface MapAgentRow {
    agent_id: string;
    name?: string;
    provider_alias?: string;
    reasoning_effort?: string;
}

/** What the backend's `_conversation_display_name` resolves from: groups, agents, peers. */
export interface ConversationNames {
    groups?: Map<string, { name?: string }> | null;
    agents?: MapAgentRow[] | null;
    peers?: Array<{ node_id: string; name?: string }> | null;
}

// --- words -----------------------------------------------------------------

export function plural(n: number, one: string, many: string): string {
    return `${n} ${n === 1 ? one : many}`;
}

function cap(s: string): string {
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

function pad(n: number): string {
    return String(n).padStart(2, '0');
}

/** Local wall-clock time of an ISO moment or epoch ms: `23:41`, or `23:41:12` with seconds. */
export function clockTime(at: string | number | null | undefined, seconds = false): string {
    const t = typeof at === 'number' ? at : at ? Date.parse(at) : NaN;
    if (!Number.isFinite(t)) return '—';
    const d = new Date(t);
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    return seconds ? `${hm}:${pad(d.getSeconds())}` : hm;
}

/** A process's run: tenths under 10 s (a git call lasts 0.2 s), formatWait words above. */
export function formatRun(seconds: number): string {
    const s = Math.max(0, seconds);
    return s < 10 ? `${s.toFixed(1)} s` : formatWait(s);
}

/** Host memory in MB (2^20 bytes, the registry's unit): `512 MB`, `1.1 GB`; null is `—`. */
export function formatMb(mb: number | null | undefined): string {
    if (typeof mb !== 'number' || !Number.isFinite(mb)) return '—';
    return mb < 1024 ? `${Math.round(mb)} MB` : `${(mb / 1024).toFixed(1)} GB`;
}

/** VRAM in MiB: `512 MiB`, `31.9 GiB`. */
export function formatMib(mib: number | null | undefined): string {
    if (typeof mib !== 'number' || !Number.isFinite(mib)) return '—';
    return mib < 1024 ? `${Math.round(mib)} MiB` : `${(mib / 1024).toFixed(1)} GiB`;
}

/** How an entry ended, as the closed exit-reason words say it; `alive` while open. */
export function endLabel(p: ProcessEntry): string {
    if (!p.ended_at) return 'alive';
    switch (p.exit_reason) {
        case 'exited': return p.exit_code === null || p.exit_code === undefined ? 'exited' : `exited ${p.exit_code}`;
        case 'stopped': return p.stop_kind ? `stopped · ${p.stop_kind}` : 'stopped';
        case null: case undefined: case '': return 'ended';
        default: return p.exit_reason;
    }
}

function endTone(p: ProcessEntry): MapProcess['endTone'] {
    if (!p.ended_at) return 'alive';
    return p.exit_reason === 'exited' ? 'ended' : 'warn';
}

function epochMs(iso: string | null | undefined): number {
    const t = iso ? Date.parse(iso) : NaN;
    return Number.isFinite(t) ? t : NaN;
}

/** Seconds a process ran: to its end, or to now while it runs. */
function runSeconds(p: ProcessEntry, nowMs: number): number {
    const start = epochMs(p.started_at);
    if (!Number.isFinite(start)) return 0;
    const end = p.ended_at ? epochMs(p.ended_at) : nowMs;
    return Number.isFinite(end) ? (end - start) / 1000 : 0;
}

// --- chats -----------------------------------------------------------------

/** A chat's name the way the backend's `_conversation_display_name` gives it:
 *  a group's name, `<agent> (1:1)` for an agent's own chat, a peer's name,
 *  else the id itself. */
export function conversationTitle(id: string | null | undefined, names: ConversationNames): string {
    if (!id) return '';
    const group = names.groups?.get(id);
    if (group) return group.name || id;
    if (id.startsWith('agent_')) {
        const agent = names.agents?.find(a => a.agent_id === id);
        if (agent?.name && agent.name !== id) return `${agent.name} (1:1)`;
    }
    return names.peers?.find(p => p.node_id === id)?.name || id;
}

/** The titles the backend already resolved in turns and waiting items, by id. */
function titlesFromTurns(turns: AgentTurnStates): Map<string, string> {
    const titles = new Map<string, string>();
    for (const s of Object.values(turns)) {
        if (s.turn?.conversation_id && s.turn.conversation_title) {
            titles.set(s.turn.conversation_id, s.turn.conversation_title);
        }
        for (const w of s.waiting) {
            if (w.conversation_id && w.conversation_title) titles.set(w.conversation_id, w.conversation_title);
        }
    }
    return titles;
}

// --- the agent's state -----------------------------------------------------

interface TurnWords {
    dot: MapDot;
    stateLine: string;
    nowLine: string;
    nowDetail: string;
}

function turnWords(state: AgentTurnState | undefined, chatOf: (id: string | null) => string, nowMs: number): TurnWords {
    const turn = state?.turn;
    if (!state || !turn) return { dot: 'idle', stateLine: 'Idle', nowLine: 'Idle', nowDetail: '' };
    const chat = turn.conversation_title || chatOf(turn.conversation_id) || 'another chat';
    const round = typeof turn.round === 'number' ? `round ${turn.round}` : '';
    const started = `since ${clockTime(turn.started_at)}`;
    const forTurn = since(turn.started_at, nowMs);
    const forWait = formatWait((nowMs - state.waitSeenAt) / 1000);
    const detail = (...parts: string[]) => parts.filter(Boolean).join(' · ');

    if (turnIsUnconfirmed(state, nowMs)) {
        const silence = silencePhrase(state, nowMs);
        return {
            dot: 'idle',
            stateLine: `Unconfirmed · ${chat} · ${silence}`,
            nowLine: `Turn in ${chat} is unconfirmed`,
            nowDetail: detail(silence, round, started),
        };
    }
    const phrase = turnWaitPhrase(turn.wait_reason);
    if (phrase && turn.wait_reason === 'model_call') {
        return {
            dot: 'waiting',
            stateLine: `${cap(phrase)} · ${chat} · ${forWait}`,
            nowLine: `${cap(phrase)} in ${chat}`,
            nowDetail: detail(turn.provider_alias ? `${turn.provider_alias} is answering` : '', round, started, forWait),
        };
    }
    if (phrase) {
        return {
            dot: 'waiting',
            stateLine: `${cap(phrase)} · ${chat} · ${forWait}`,
            nowLine: `${cap(phrase)} in ${chat}`,
            nowDetail: detail(turn.tool ?? '', `asked at ${clockTime(state.waitSeenAt)}`, forWait),
        };
    }
    const doing = turn.tool ? `Running ${turn.tool}` : 'Working';
    return {
        dot: 'working',
        stateLine: `${doing} · ${chat} · ${forTurn}`,
        nowLine: `${doing} in ${chat}`,
        nowDetail: detail(turn.tool ?? '', round, started, forTurn),
    };
}

function waitingChats(state: AgentTurnState | undefined, chatOf: (id: string | null) => string,
                      nameOf: (id: string) => string, nowMs: number): MapWaitingChat[] {
    if (!state) return [];
    // The position counts the chats behind the agent's own gate, as the chat's line does.
    const queue = state.waiting.filter(w => w.wait_reason === 'other_chat');
    return state.waiting.map(w => ({
        conversation_id: w.conversation_id,
        chat: w.conversation_title || chatOf(w.conversation_id),
        place: w.wait_reason === 'other_chat' ? `${ordinal(queue.indexOf(w) + 1)} in line` : '',
        reason: `${waitingReasonPhrase(w, nameOf)}${heldByApprovalSuffix(w)}`,
        wait: since(w.waiting_since, nowMs),
    }));
}

// --- processes -------------------------------------------------------------

/** Under an agent: a tool's spawn whose spawn origin names an agent. A subsystem
 *  belongs to the backend even when a tool call started it (GLOSSARY, process
 *  registry), and a tool spawn with no origin is a lost attribution, shown in the
 *  system branch under its launcher rather than hidden. */
export function isAgentProcess(p: ProcessEntry): boolean {
    return p.launcher_kind === 'tool' && !!p.agent_id;
}

/** Alive first, then newest started first. */
function byAliveThenNewest(a: ProcessEntry, b: ProcessEntry): number {
    const alive = Number(!b.ended_at) - Number(!a.ended_at);
    if (alive) return alive;
    return (epochMs(b.started_at) || 0) - (epochMs(a.started_at) || 0);
}

function inAgentWindow(p: ProcessEntry, nowMs: number): boolean {
    if (!p.ended_at) return true;
    const ended = epochMs(p.ended_at);
    return Number.isFinite(ended) && nowMs - ended <= AGENT_PROCESS_WINDOW_S * 1000;
}

function processRow(p: ProcessEntry, chatOf: (id: string | null) => string, nowMs: number): MapProcess {
    const alive = !p.ended_at;
    return {
        pid: p.pid,
        command: p.command || p.launcher,
        chat: chatOf(p.conversation_id),
        started: clockTime(p.started_at, true),
        duration: formatRun(runSeconds(p, nowMs)),
        rss: formatMb(p.peak_rss_mb),
        // A live entry's commit is read at the request; an ended one keeps its peak.
        commit: formatMb(alive ? (p.commit_mb ?? p.peak_commit_mb) : p.peak_commit_mb),
        end: endLabel(p),
        alive,
        endTone: endTone(p),
    };
}

/** The launchers the system branch names, in its order; any other is shown by its own name. */
export const SYSTEM_LAUNCHER_LABELS: Record<string, string> = {
    strata: 'Strata launcher',
    llamacpp_server: 'llama-server',
    browser: 'Browser',
    device_probe: 'Device probes',
    git_helper: 'Git helpers',
};

function launcherOrder(launcher: string): number {
    const i = Object.keys(SYSTEM_LAUNCHER_LABELS).indexOf(launcher);
    return i < 0 ? Number.MAX_SAFE_INTEGER : i;
}

/** How a set of finished entries ended: one word when they agree, counts when not. */
function endsSummary(ended: ProcessEntry[]): string {
    const counts = new Map<string, number>();
    for (const p of ended) counts.set(endLabel(p), (counts.get(endLabel(p)) ?? 0) + 1);
    if (counts.size === 1) return [...counts.keys()][0];
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([label, n]) => `${n} ${label}`).join(', ');
}

function systemItems(entries: ProcessEntry[], nowMs: number): MapSystemItem[] {
    const byLauncher = new Map<string, ProcessEntry[]>();
    for (const p of entries) {
        const list = byLauncher.get(p.launcher) ?? [];
        list.push(p);
        byLauncher.set(p.launcher, list);
    }
    const launchers = [...byLauncher.keys()].sort((a, b) => launcherOrder(a) - launcherOrder(b) || a.localeCompare(b));
    const items: MapSystemItem[] = [];
    for (const launcher of launchers) {
        const label = SYSTEM_LAUNCHER_LABELS[launcher] ?? launcher;
        const list = byLauncher.get(launcher)!.slice().sort(byAliveThenNewest);
        for (const p of list.filter(e => !e.ended_at)) {
            const parts = ['alive', formatRun(runSeconds(p, nowMs))];
            if (p.peak_rss_mb != null) parts.push(`RSS ${formatMb(p.peak_rss_mb)}`);
            const commit = p.commit_mb ?? p.peak_commit_mb;
            if (commit != null) parts.push(`commit ${formatMb(commit)}`);
            if (p.peak_vram_mib != null) parts.push(`VRAM ${formatMib(p.peak_vram_mib)}`);
            items.push({ name: `${label} · pid ${p.pid}`, line: parts.join(' · '), alive: true });
        }
        const ended = list.filter(e => !!e.ended_at);
        if (ended.length) {
            const last = ended.reduce((a, b) => ((epochMs(b.ended_at) || 0) > (epochMs(a.ended_at) || 0) ? b : a));
            const total = ended.reduce((sum, p) => sum + runSeconds(p, nowMs), 0);
            items.push({
                name: `${label} · ${ended.length}`,
                line: `ended · last at ${clockTime(last.ended_at)} · ${endsSummary(ended)} · ${formatRun(total)} in all`,
                alive: false,
            });
        }
    }
    return items;
}

// --- the whole map ---------------------------------------------------------

/** The busy and waiting counts of the AGENTS MAP button; empty when nothing is busy or waiting. */
export function buttonSummary(turns: AgentTurnStates): string {
    const states = Object.values(turns);
    const busy = states.filter(s => !!s.turn).length;
    const waiting = states.reduce((n, s) => n + s.waiting.length, 0);
    if (!busy && !waiting) return '';
    return `${busy} busy · ${plural(waiting, 'chat waiting', 'chats waiting')}`;
}

/** What changes the map's processes: the turns and waiting lists, never the
 *  activity clock. A new key is a reason to read `list_processes` again. */
export function turnsKey(turns: AgentTurnStates): string {
    return JSON.stringify(Object.keys(turns).sort().map(id => [id, turns[id].turn, turns[id].waiting]));
}

/** The selected agent: the one asked for while it is on the map, else the first. */
export function pickSelected(map: AgentMap, selectedId: string | null | undefined): string | null {
    if (selectedId && map.agents.some(a => a.agent_id === selectedId)) return selectedId;
    return map.agents[0]?.agent_id ?? null;
}

function memory(reading: FreeCommitReading | null | undefined): { bars: MapMemoryBar[]; source: string } {
    if (!reading) return { bars: [], source: '' };
    const n = (v: number) => v.toLocaleString('en-US');
    const bar = (kind: MapMemoryBar['kind'], label: string, avail: number | null, total: number | null): MapMemoryBar[] =>
        typeof avail === 'number' && typeof total === 'number' && total > 0
            ? [{ kind, label: `${label} ${n(avail)} of ${n(total)} MiB`, fraction: Math.min(1, Math.max(0, avail / total)) }]
            : [];
    return {
        bars: [
            ...bar('commit', 'Free commit', reading.avail_page_file_mib, reading.total_page_file_mib),
            ...bar('physical', 'Free physical', reading.avail_phys_mib, reading.total_phys_mib),
        ],
        source: reading.source ? `Read now · ${reading.source}` : 'Read now',
    };
}

const DOT_RANK: Record<MapDot, number> = { working: 0, waiting: 1, idle: 2 };

export function buildAgentMap(input: {
    agents: MapAgentRow[];
    turns: AgentTurnStates;
    listing: ProcessListing | null;
    names: ConversationNames;
    nowMs: number;
}): AgentMap {
    const { turns, listing, nowMs } = input;
    const known = titlesFromTurns(turns);
    const names: ConversationNames = { ...input.names, agents: input.names.agents ?? input.agents };
    const chatOf = (id: string | null) => (id ? known.get(id) || conversationTitle(id, names) : '');
    const nameById = new Map(input.agents.map(a => [a.agent_id, a.name || a.agent_id]));
    const nameOf = (id: string) => nameById.get(id) || id;

    const processes = listing?.processes ?? [];
    const agentProcs = new Map<string, ProcessEntry[]>();
    for (const p of processes.filter(isAgentProcess)) {
        const list = agentProcs.get(p.agent_id!) ?? [];
        list.push(p);
        agentProcs.set(p.agent_id!, list);
    }

    // A process of an agent no longer on the list still shows, under its id.
    const rows: MapAgentRow[] = [...input.agents];
    for (const id of agentProcs.keys()) {
        if (!nameById.has(id)) rows.push({ agent_id: id, name: id });
    }

    const agents = rows.map((row): MapAgent => {
        const state = turns[row.agent_id];
        const words = turnWords(state, chatOf, nowMs);
        const procs = (agentProcs.get(row.agent_id) ?? [])
            .filter(p => inAgentWindow(p, nowMs))
            .sort(byAliveThenNewest)
            .map(p => processRow(p, chatOf, nowMs));
        return {
            agent_id: row.agent_id,
            name: row.name || row.agent_id,
            model: row.provider_alias ?? '',
            effort: row.reasoning_effort ?? '',
            ...words,
            waiting: waitingChats(state, chatOf, nameOf, nowMs),
            processes: procs,
            alive: procs.filter(p => p.alive).length,
        };
    });
    // Working, then waiting, then idle; the list's own order inside each.
    const ordered = agents
        .map((a, i) => ({ a, i }))
        .sort((x, y) => DOT_RANK[x.a.dot] - DOT_RANK[y.a.dot] || x.i - y.i)
        .map(x => x.a);

    const systemEntries = processes.filter(p => !isAgentProcess(p));
    const system = systemItems(systemEntries, nowMs);
    const systemAlive = systemEntries.filter(p => !p.ended_at).length;
    const vram = systemEntries.filter(p => !p.ended_at && p.peak_vram_mib != null)
        .reduce((sum, p) => sum + (p.peak_vram_mib ?? 0), 0);
    const launcherLabels = [...new Set(system.map(s => s.name.split(' · ')[0]))];
    const systemSummary = [...launcherLabels, `${systemAlive} alive`, ...(vram > 0 ? [`VRAM ${formatMib(vram)}`] : [])]
        .join(' · ');

    const alive = processes.filter(p => !p.ended_at).length;
    const busy = ordered.filter(a => !!turns[a.agent_id]?.turn).length;
    const waiting = ordered.reduce((n, a) => n + a.waiting.length, 0);
    const backendStart = listing?.backend_start_time;
    const backendLine = listing
        ? [
            listing.backend_pid != null ? `pid ${listing.backend_pid}` : '',
            typeof backendStart === 'number' ? `up ${formatWait((nowMs - backendStart * 1000) / 1000)}` : '',
            `${plural(alive, 'process', 'processes')} alive`,
        ].filter(Boolean).join(' · ')
        : '';
    const summaryLine = [
        plural(ordered.length, 'agent', 'agents'),
        `${busy} busy`,
        plural(waiting, 'chat waiting', 'chats waiting'),
        ...(listing ? [`${plural(alive, 'process', 'processes')} alive`] : []),
        'click an agent for details',
    ].join(' · ');
    const mem = memory(listing?.free_commit);

    return {
        agents: ordered,
        system,
        systemSummary,
        backendLine,
        summaryLine,
        memoryBars: mem.bars,
        memorySource: mem.source,
    };
}
