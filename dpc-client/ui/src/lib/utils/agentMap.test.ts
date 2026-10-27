import { describe, it, expect } from 'vitest';
import {
    AGENT_PROCESS_WINDOW_S,
    PROCESS_POLL_MS,
    SYSTEM_OPEN_BY_DEFAULT,
    buildAgentMap,
    buttonSummary,
    clockTime,
    conversationTitle,
    endLabel,
    formatMb,
    formatRun,
    isAgentProcess,
    pickSelected,
    turnsKey,
    type MapAgentRow,
    type ProcessEntry,
    type ProcessListing,
} from './agentMap';
import { applyTurnSnapshot, noteAgentActivity, type AgentTurn, type AgentTurnStates } from './agentTurnNotice';

const NOW = Date.parse('2026-10-27T10:00:00Z');
const iso = (secondsAgo: number) => new Date(NOW - secondsAgo * 1000).toISOString();

const AGENTS: MapAgentRow[] = [
    { agent_id: 'agent_iris', name: 'Iris', provider_alias: 'qwen', reasoning_effort: 'low' },
    { agent_id: 'agent_warren', name: 'Warren', provider_alias: 'strata', reasoning_effort: 'medium' },
    { agent_id: 'agent_ark', name: 'Ark', provider_alias: 'deepseek_flash', reasoning_effort: 'high' },
];

function turn(over: Partial<AgentTurn> = {}): AgentTurn {
    return {
        conversation_id: 'group-a', conversation_title: 'DPC Project', started_at: iso(134),
        round: 7, provider_alias: null, tool: null, wait_reason: null, ...over,
    };
}

function states(snaps: any[], at = NOW): AgentTurnStates {
    return snaps.reduce((m, s) => applyTurnSnapshot(m, s, at), {} as AgentTurnStates);
}

function proc(over: Partial<ProcessEntry> = {}): ProcessEntry {
    return {
        pid: 100, start_time: 1, parent_pid: 1, parent_start_time: 1,
        launcher: 'run_shell', launcher_kind: 'tool', agent_id: 'agent_ark', conversation_id: 'group-a',
        tool: 'run_shell', round: 7, command: 'pytest -q', started_at: iso(60), ended_at: null,
        exit_reason: null, stop_kind: null, exit_code: null, signal: null,
        peak_rss_mb: 512, peak_commit_mb: 700, peak_vram_mib: null, commit_mb: 610, ...over,
    };
}

function listing(processes: ProcessEntry[], over: Partial<ProcessListing> = {}): ProcessListing {
    return {
        processes, backend_pid: 34292, backend_start_time: (NOW - 7500 * 1000) / 1000,
        free_commit: {
            avail_page_file_mib: 41210, total_page_file_mib: 108236,
            avail_phys_mib: 11305, total_phys_mib: 63168, source: 'GlobalMemoryStatusEx',
        },
        ...over,
    };
}

const build = (turns: AgentTurnStates, l: ProcessListing | null = null, agents = AGENTS) =>
    buildAgentMap({ agents, turns, listing: l, names: { groups: new Map([['group-b', { name: 'Hardware' }]]) }, nowMs: NOW });

describe('the agents and their turns', () => {
    const turns = () => states([
        { agent_id: 'agent_ark', ts: 't1', turn: turn({ tool: 'run_shell' }), waiting: [] },
        { agent_id: 'agent_warren', ts: 't1', turn: turn({ conversation_id: 'group-b', conversation_title: 'Hardware',
          wait_reason: 'model_call', provider_alias: 'strata flash-next', round: 1 }), waiting: [] },
        { agent_id: 'agent_iris', ts: 't1', turn: null, waiting: [] },
    ], NOW - 48_000);

    it('puts working agents first, then waiting, then idle', () => {
        const map = build(turns());
        expect(map.agents.map(a => [a.name, a.dot])).toEqual([
            ['Ark', 'working'], ['Warren', 'waiting'], ['Iris', 'idle'],
        ]);
    });

    it('keeps the list order among agents in the same state', () => {
        const map = build({});
        expect(map.agents.map(a => a.name)).toEqual(['Iris', 'Warren', 'Ark']);
    });

    it('says what a turn is doing, in which chat, for how long', () => {
        const map = build(turns());
        const ark = map.agents.find(a => a.name === 'Ark')!;
        expect(ark.stateLine).toBe('Running run_shell · DPC Project · 2 min 14 s');
        expect(ark.nowLine).toBe('Running run_shell in DPC Project');
        expect(ark.nowDetail).toBe(`run_shell · round 7 · since ${clockTime(iso(134))} · 2 min 14 s`);
        const warren = map.agents.find(a => a.name === 'Warren')!;
        expect(warren.stateLine).toBe('Waiting for the model · Hardware · 48 s');
        expect(warren.nowDetail).toBe(`strata flash-next is answering · round 1 · since ${clockTime(iso(134))} · 48 s`);
        expect(map.agents.find(a => a.name === 'Iris')!.stateLine).toBe('Idle');
    });

    it('says an approval wait and when it was asked', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [],
                             turn: turn({ wait_reason: 'approval', tool: 'run_shell' }) }], NOW - 182_000 + 120_000);
        const ark = build(noteAgentActivity(st, 'agent_ark', NOW)).agents[0];
        expect(ark.dot).toBe('waiting');
        expect(ark.stateLine).toBe('Waiting for an approval · DPC Project · 1 min 2 s');
        expect(ark.nowDetail).toBe(`run_shell · asked at ${clockTime(NOW - 62_000)} · 1 min 2 s`);
    });

    it('shows a turn silent for longer than 120 s as unconfirmed, not working', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [], turn: turn() }], NOW - 121_000);
        const ark = build(st).agents.find(a => a.name === 'Ark')!;
        expect(ark.dot).toBe('idle');
        expect(ark.stateLine).toBe('Unconfirmed · DPC Project · nothing from it for 2 min 1 s');
    });

    it('lists the chats waiting, in arrival order, with the place in line and why', () => {
        const st = states([{
            agent_id: 'agent_ark', ts: 't1', turn: turn(),
            waiting: [
                { conversation_id: 'agent_ark', conversation_title: 'Ark (1:1)', waiting_since: iso(65),
                  wait_reason: 'other_chat', held_by: { conversation_id: 'group-a', wait_reason: 'approval' } },
                { conversation_id: 'group-b', conversation_title: 'Hardware', waiting_since: iso(30),
                  wait_reason: 'room_busy', held_by: { conversation_id: 'group-b', wait_reason: null, agent_id: 'agent_warren' } },
                { conversation_id: 'peer-1', conversation_title: '', waiting_since: iso(22),
                  wait_reason: 'other_chat', held_by: { conversation_id: 'group-a', wait_reason: null } },
            ],
        }]);
        const ark = build(st).agents[0];
        expect(ark.waiting).toEqual([
            { conversation_id: 'agent_ark', chat: 'Ark (1:1)', place: '1st in line',
              reason: 'busy in another chat, which waits for an approval', wait: '1 min 5 s' },
            { conversation_id: 'group-b', chat: 'Hardware', place: '',
              reason: 'Warren is working in this room', wait: '30 s' },
            { conversation_id: 'peer-1', chat: 'peer-1', place: '2nd in line',
              reason: 'busy in another chat', wait: '22 s' },
        ]);
    });
});

describe('the processes under an agent', () => {
    it('only a tool spawn whose origin names an agent', () => {
        expect(isAgentProcess(proc())).toBe(true);
        expect(isAgentProcess(proc({ launcher_kind: 'subsystem', launcher: 'browser' }))).toBe(false);
        expect(isAgentProcess(proc({ agent_id: null }))).toBe(false);
    });

    it('alive first, then the newest started; an end older than the window drops out', () => {
        const map = build({}, listing([
            proc({ pid: 1, started_at: iso(3000), ended_at: iso(2990), exit_reason: 'exited', exit_code: 0 }),
            proc({ pid: 2, started_at: iso(1800) }),
            proc({ pid: 3, started_at: iso(100), ended_at: iso(99), exit_reason: 'timeout' }),
            proc({ pid: 4, started_at: iso(60) }),
            proc({ pid: 5, started_at: iso(9000), ended_at: iso(AGENT_PROCESS_WINDOW_S + 1), exit_reason: 'exited', exit_code: 0 }),
        ]));
        const ark = map.agents.find(a => a.name === 'Ark')!;
        expect(ark.processes.map(p => p.pid)).toEqual([4, 2, 3, 1]);
        expect(ark.alive).toBe(2);
    });

    it('prints a row: the chat by name, the run, the memory, how it ended', () => {
        const map = build({}, listing([
            proc({ pid: 7, conversation_id: 'group-b', started_at: iso(31 * 60) }),
            proc({ pid: 8, started_at: iso(10), ended_at: iso(9.8), exit_reason: 'exited', exit_code: 0,
                   peak_rss_mb: 21, peak_commit_mb: 5, commit_mb: null }),
        ]));
        const [live, done] = map.agents.find(a => a.name === 'Ark')!.processes;
        expect(live).toMatchObject({ pid: 7, chat: 'Hardware', duration: '31 min 0 s', rss: '512 MB',
                                     commit: '610 MB', end: 'alive', endTone: 'alive', alive: true });
        expect(live.started).toBe(clockTime(iso(31 * 60), true));
        expect(done).toMatchObject({ duration: '0.2 s', rss: '21 MB', commit: '5 MB', end: 'exited 0', endTone: 'ended' });
    });

    it('a live row with no commit reading falls back to the peak', () => {
        const map = build({}, listing([proc({ commit_mb: null, peak_commit_mb: 1331 })]));
        expect(map.agents.find(a => a.name === 'Ark')!.processes[0].commit).toBe('1.3 GB');
    });

    it('an agent no longer on the list still shows its processes, under its id', () => {
        const map = build({}, listing([proc({ agent_id: 'agent_gone' })]));
        expect(map.agents.map(a => a.agent_id)).toContain('agent_gone');
        expect(map.agents.find(a => a.agent_id === 'agent_gone')!.processes).toHaveLength(1);
    });
});

describe('how an entry ended', () => {
    it('uses the closed exit-reason words', () => {
        expect(endLabel(proc())).toBe('alive');
        expect(endLabel(proc({ ended_at: iso(1), exit_reason: 'exited', exit_code: 2 }))).toBe('exited 2');
        expect(endLabel(proc({ ended_at: iso(1), exit_reason: 'exited', exit_code: null }))).toBe('exited');
        expect(endLabel(proc({ ended_at: iso(1), exit_reason: 'stopped', stop_kind: 'ui_stop' }))).toBe('stopped · ui_stop');
        expect(endLabel(proc({ ended_at: iso(1), exit_reason: 'memory_ceiling' }))).toBe('memory_ceiling');
        expect(endLabel(proc({ ended_at: iso(1), exit_reason: null }))).toBe('ended');
    });
});

describe('the system branch', () => {
    const sys = (over: Partial<ProcessEntry>) =>
        proc({ launcher_kind: 'subsystem', agent_id: null, conversation_id: null, tool: null, ...over });

    it('opens collapsed', () => {
        expect(SYSTEM_OPEN_BY_DEFAULT).toBe(false);
    });

    it('one item per live engine, finished ones counted per launcher, in the launcher order', () => {
        const map = build({}, listing([
            sys({ pid: 31, launcher: 'git_helper', started_at: iso(500), ended_at: iso(499.6), exit_reason: 'exited', exit_code: 0 }),
            sys({ pid: 32, launcher: 'git_helper', started_at: iso(300), ended_at: iso(299.8), exit_reason: 'exited', exit_code: 0 }),
            sys({ pid: 21, launcher: 'device_probe', started_at: iso(7000), ended_at: iso(6999), exit_reason: 'exited', exit_code: 0 }),
            sys({ pid: 22, launcher: 'device_probe', started_at: iso(7000), ended_at: iso(6990), exit_reason: 'timeout' }),
            sys({ pid: 49120, launcher: 'strata', started_at: iso(7260), peak_rss_mb: 32051, commit_mb: 67584,
                  peak_vram_mib: 32666 }),
            // A subsystem a tool call started keeps its origin and still belongs to the backend.
            sys({ pid: 5512, launcher: 'browser', agent_id: 'agent_ark', started_at: iso(2460), peak_rss_mb: 812,
                  commit_mb: 1126 }),
        ]));
        expect(map.system).toEqual([
            { name: 'Strata launcher · pid 49120', alive: true,
              line: 'alive · 2 h 1 min · RSS 31.3 GB · commit 66.0 GB · VRAM 31.9 GiB' },
            { name: 'Browser · pid 5512', alive: true, line: 'alive · 41 min 0 s · RSS 812 MB · commit 1.1 GB' },
            { name: 'Device probes · 2', alive: false,
              line: `ended · last at ${clockTime(iso(6990))} · 1 exited 0, 1 timeout · 11 s in all` },
            { name: 'Git helpers · 2', alive: false,
              line: `ended · last at ${clockTime(iso(299.8))} · exited 0 · 0.6 s in all` },
        ]);
        expect(map.systemSummary).toBe('Strata launcher · Browser · Device probes · Git helpers · 2 alive · VRAM 31.9 GiB');
        expect(map.agents.find(a => a.name === 'Ark')!.processes).toEqual([]);
    });

    it('a tool spawn with no origin is shown there under its launcher, never dropped', () => {
        const map = build({}, listing([proc({ agent_id: null, pid: 77 })]));
        expect(map.system.map(s => s.name)).toEqual(['run_shell · pid 77']);
    });
});

describe('the header, the backend and memory', () => {
    it('counts agents, busy, chats waiting and live processes', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', turn: turn(), waiting: [
            { conversation_id: 'c1', conversation_title: 'x', waiting_since: iso(5), wait_reason: 'other_chat',
              held_by: { conversation_id: 'group-a', wait_reason: null } }] }]);
        const map = build(st, listing([proc(), proc({ pid: 2, ended_at: iso(1), exit_reason: 'exited', exit_code: 0 })]));
        expect(map.summaryLine).toBe('3 agents · 1 busy · 1 chat waiting · 1 process alive · click an agent for details');
        expect(map.backendLine).toBe('pid 34292 · up 2 h 5 min · 1 process alive');
        expect(build(st).summaryLine).toBe('3 agents · 1 busy · 1 chat waiting · click an agent for details');
        expect(build(st).backendLine).toBe('');
    });

    it('free commit and free physical on Windows, physical alone elsewhere, nothing unread', () => {
        const win = build({}, listing([])).memoryBars;
        expect(win.map(b => b.label)).toEqual(['Free commit 41,210 of 108,236 MiB', 'Free physical 11,305 of 63,168 MiB']);
        expect(win[0].fraction).toBeCloseTo(41210 / 108236);
        const posix = build({}, listing([], { free_commit: {
            avail_page_file_mib: null, total_page_file_mib: null, avail_phys_mib: 900, total_phys_mib: 1000, source: 'psutil' } }));
        expect(posix.memoryBars.map(b => b.label)).toEqual(['Free physical 900 of 1,000 MiB']);
        expect(posix.memorySource).toBe('Read now · psutil');
        expect(build({}, listing([], { free_commit: null })).memoryBars).toEqual([]);
    });
});

describe('chats by name', () => {
    it('as the backend names them: group, an agent\'s own chat, a peer, else the id', () => {
        const names = { groups: new Map([['g1', { name: 'Hardware' }]]), agents: AGENTS,
                        peers: [{ node_id: 'dpc-node-1', name: 'Linus' }] };
        expect(conversationTitle('g1', names)).toBe('Hardware');
        expect(conversationTitle('agent_ark', names)).toBe('Ark (1:1)');
        expect(conversationTitle('dpc-node-1', names)).toBe('Linus');
        expect(conversationTitle('elsewhere', names)).toBe('elsewhere');
        expect(conversationTitle(null, names)).toBe('');
    });

    it('a title the backend gave in a turn wins over the local guess', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [],
                             turn: turn({ conversation_id: 'group-b', conversation_title: 'Hardware (renamed)' }) }]);
        const map = build(st, listing([proc({ conversation_id: 'group-b' })]));
        expect(map.agents.find(a => a.name === 'Ark')!.processes[0].chat).toBe('Hardware (renamed)');
    });
});

describe('the button, the refresh and the selection', () => {
    it('the button counts busy agents and waiting chats, and says nothing when all are idle', () => {
        expect(buttonSummary({})).toBe('');
        const st = states([
            { agent_id: 'a', ts: 't', turn: turn(), waiting: [] },
            { agent_id: 'b', ts: 't', turn: null, waiting: [
                { conversation_id: 'c', conversation_title: '', waiting_since: iso(1), wait_reason: 'room_busy', held_by: null },
                { conversation_id: 'd', conversation_title: '', waiting_since: iso(1), wait_reason: 'room_busy', held_by: null }] },
        ]);
        expect(buttonSummary(st)).toBe('1 busy · 2 chats waiting');
    });

    it('a turn change is a reason to read the processes again; an activity tick is not', () => {
        const st = states([{ agent_id: 'agent_ark', ts: 't1', waiting: [], turn: turn() }]);
        expect(turnsKey(noteAgentActivity(st, 'agent_ark', NOW + 5000))).toBe(turnsKey(st));
        const next = applyTurnSnapshot(st, { agent_id: 'agent_ark', ts: 't2', waiting: [], turn: turn({ tool: 'run_shell' }) }, NOW);
        expect(turnsKey(next)).not.toBe(turnsKey(st));
    });

    it('keeps the chosen agent while it is on the map, else the first', () => {
        const map = build({});
        expect(pickSelected(map, 'agent_ark')).toBe('agent_ark');
        expect(pickSelected(map, 'agent_gone')).toBe('agent_iris');
        expect(pickSelected(map, null)).toBe('agent_iris');
        expect(pickSelected(build({}, null, []), null)).toBeNull();
    });

    it('polls at a modest rate', () => {
        expect(PROCESS_POLL_MS).toBeGreaterThanOrEqual(2000);
    });
});

describe('words', () => {
    it('runs and memory', () => {
        expect(formatRun(0.24)).toBe('0.2 s');
        expect(formatRun(30)).toBe('30 s');
        expect(formatMb(null)).toBe('—');
        expect(formatMb(1126)).toBe('1.1 GB');
    });
});
