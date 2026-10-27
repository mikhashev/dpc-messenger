import { describe, it, expect } from 'vitest';
import {
    AGENT_PROCESS_WINDOW_S,
    FINISHED_CUT_NOTE,
    LIST_PROCESSES_FINISHED_MAX,
    PROCESS_POLL_MS,
    SYSTEM_OPEN_BY_DEFAULT,
    VRAM_NOTE,
    VRAM_READING_TTL_S,
    agentStatusLine,
    buildAgentMap,
    buttonSummary,
    COMMAND_CUT_NOTE,
    COMMAND_MAX_CHARS,
    chatNamer,
    clockTime,
    conversationTitle,
    endLabel,
    finishedListCut,
    formatBytes,
    formatMb,
    formatRun,
    isAgentProcess,
    pickSelected,
    processDetail,
    processKey,
    toggleExpanded,
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
        vram_free_mib: null, vram_total_mib: null,
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
        expect(map.system.map(({ key, details, ...item }) => item)).toEqual([
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

    it('free VRAM is a third bar when the card was read, and absent when it was not', () => {
        const withCard = build({}, listing([], { vram_free_mib: 1234, vram_total_mib: 32607 })).memoryBars;
        expect(withCard.map(b => b.label)).toEqual([
            'Free commit 41,210 of 108,236 MiB', 'Free physical 11,305 of 63,168 MiB', 'Free VRAM 1,234 of 32,607 MiB']);
        expect(withCard[2]).toMatchObject({ kind: 'vram' });
        expect(withCard[2].fraction).toBeCloseTo(1234 / 32607);
        expect(build({}, listing([])).memoryBars.map(b => b.kind)).toEqual(['commit', 'physical']);
        expect(build({}, listing([], { vram_free_mib: 5, vram_total_mib: null })).memoryBars.map(b => b.kind))
            .toEqual(['commit', 'physical']);
    });

    it('the card alone still makes a bar; "Read now" is not said for it', () => {
        const map = build({}, listing([], { free_commit: null, vram_free_mib: 100, vram_total_mib: 200 }));
        expect(map.memoryBars.map(b => b.label)).toEqual(['Free VRAM 100 of 200 MiB']);
        expect(map.memorySource).toBe('');
    });

    it('the VRAM bar says its reading may be up to 30 s old; the host bars carry no note', () => {
        const map = build({}, listing([], { vram_free_mib: 1234, vram_total_mib: 32607 }));
        expect(VRAM_READING_TTL_S).toBe(30);
        expect(VRAM_NOTE).toBe('read ≤30 s ago');
        expect(map.memoryBars.map(b => [b.kind, b.note])).toEqual([
            ['commit', ''], ['physical', ''], ['vram', 'read ≤30 s ago']]);
        expect(map.memorySource).toBe('Read now · GlobalMemoryStatusEx');
    });
});

describe('the 200 finished the backend returns', () => {
    const finished = (n: number, endedAgo: (i: number) => number) =>
        Array.from({ length: n }, (_, i) => proc({ pid: 1000 + i, started_at: iso(endedAgo(i) + 5),
            ended_at: iso(endedAgo(i)), exit_reason: 'exited', exit_code: 0 }));

    it('is cut when 200 finished are listed and the oldest is younger than the hour window', () => {
        expect(LIST_PROCESSES_FINISHED_MAX).toBe(200);
        const full = finished(200, i => 10 + i * 10);   // oldest ended 1 990 s ago
        expect(finishedListCut(full, NOW)).toBe(true);
        const map = build({}, listing(full));
        expect(map.finishedCapped).toBe(true);
        expect(map.finishedCapNote).toBe(FINISHED_CUT_NOTE);
        expect(FINISHED_CUT_NOTE).toBe('Showing the latest 200 finished; older ones in the last hour are not listed.');
        expect(map.summaryLine).toContain('finished: latest 200 only');
    });

    it('is not cut when the oldest finished is already past the window', () => {
        const full = finished(200, i => AGENT_PROCESS_WINDOW_S + 10 + i);
        expect(finishedListCut(full, NOW)).toBe(false);
        expect(build({}, listing(full)).finishedCapNote).toBe('');
    });

    it('is not cut below 200, and open entries do not count toward the 200', () => {
        expect(finishedListCut(finished(199, i => 10 + i), NOW)).toBe(false);
        const open = Array.from({ length: 5 }, (_, i) => proc({ pid: 5000 + i }));
        expect(finishedListCut([...finished(199, i => 10 + i), ...open], NOW)).toBe(false);
        const map = build({}, listing([...finished(199, i => 10 + i), ...open]));
        expect(map.finishedCapped).toBe(false);
        expect(map.summaryLine).not.toContain('finished:');
    });

    it('judges the oldest by its end, whatever the order of the list', () => {
        const full = finished(200, i => 10 + i * 10).reverse();
        expect(finishedListCut(full, NOW)).toBe(true);
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

describe('the sidebar names a chat as the map does', () => {
    const names = { groups: new Map([['group-b', { name: 'Hardware' }]]), agents: AGENTS,
                    peers: [{ node_id: 'dpc-node-1', name: 'Linus' }] };
    const empty = (id: string) => states([{ agent_id: 'agent_ark', ts: 't1', waiting: [],
        turn: turn({ conversation_id: id, conversation_title: '' }) }], NOW - 48_000);

    it('an empty title is resolved, not printed as the id', () => {
        for (const [id, name] of [['group-b', 'Hardware'], ['agent_iris', 'Iris (1:1)'], ['dpc-node-1', 'Linus']]) {
            const turns = empty(id);
            const sidebar = agentStatusLine(turns['agent_ark'], NOW, chatNamer(turns, names));
            const mapped = buildAgentMap({ agents: AGENTS, turns, listing: null, names, nowMs: NOW }).agents.find(a => a.agent_id === 'agent_ark')!;
            expect(sidebar.line).toBe(`Working · ${name} · 2 min 14 s`);
            expect(sidebar.line).toBe(mapped.stateLine);
        }
    });

    it('a title the backend gave in any turn wins, and an unknown chat keeps its id', () => {
        const turns = states([
            { agent_id: 'agent_ark', ts: 't1', waiting: [], turn: turn({ conversation_id: 'group-b', conversation_title: '' }) },
            { agent_id: 'agent_iris', ts: 't1', waiting: [], turn: turn({ conversation_id: 'group-b', conversation_title: 'Hardware (renamed)' }) },
        ], NOW - 48_000);
        expect(agentStatusLine(turns['agent_ark'], NOW, chatNamer(turns, names)).line).toBe('Working · Hardware (renamed) · 2 min 14 s');
        const lost = empty('elsewhere');
        expect(agentStatusLine(lost['agent_ark'], NOW, chatNamer(lost, names)).line).toBe('Working · elsewhere · 2 min 14 s');
    });

    it('without a namer the id stands, as before', () => {
        expect(agentStatusLine(empty('group-b')['agent_ark'], NOW).line).toBe('Working · group-b · 2 min 14 s');
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

describe('the line under an agent in the sidebar', () => {
    const line = (snap: any) => agentStatusLine(states([snap], NOW - 48_000)['agent_ark'], NOW);

    it('says the map words for the same turn', () => {
        const snap = { agent_id: 'agent_ark', ts: 't1', turn: turn({ tool: 'run_shell' }), waiting: [] };
        const map = build(states([snap], NOW - 48_000));
        const ark = map.agents.find(a => a.agent_id === 'agent_ark')!;
        expect(line(snap)).toEqual({ dot: ark.dot, line: ark.stateLine, waiting: '' });
        expect(line(snap).line).toBe('Running run_shell · DPC Project · 2 min 14 s');
    });

    it('waiting for the model or an approval is amber with the wait time', () => {
        const wait = (r: any) => line({ agent_id: 'agent_ark', ts: 't1', turn: turn({ wait_reason: r }), waiting: [] });
        expect(wait('model_call')).toMatchObject({ dot: 'waiting', line: 'Waiting for the model · DPC Project · 48 s' });
        expect(wait('approval').dot).toBe('waiting');
        expect(wait('approval').line).toMatch(/^Waiting for an approval · DPC Project · /);
    });

    it('counts the chats waiting beside the state', () => {
        const waiting = [
            { conversation_id: 'c1', conversation_title: 'x', waiting_since: iso(5), wait_reason: 'other_chat', held_by: null },
            { conversation_id: 'c2', conversation_title: 'y', waiting_since: iso(9), wait_reason: 'other_chat', held_by: null },
        ];
        expect(line({ agent_id: 'agent_ark', ts: 't1', turn: turn(), waiting }).waiting).toBe('2 chats waiting');
        expect(line({ agent_id: 'agent_ark', ts: 't1', turn: null, waiting: [waiting[0]] }).waiting).toBe('1 chat waiting');
    });

    it('an agent with no turn, or none known, is idle', () => {
        expect(line({ agent_id: 'agent_ark', ts: 't1', turn: null, waiting: [] })).toEqual({ dot: 'idle', line: 'Idle', waiting: '' });
        expect(agentStatusLine(undefined, NOW)).toEqual({ dot: 'idle', line: 'Idle', waiting: '' });
    });

    it('a turn silent past the limit is unconfirmed, grey', () => {
        const state = states([{ agent_id: 'agent_ark', ts: 't1', turn: turn(), waiting: [] }], NOW - 300_000)['agent_ark'];
        const out = agentStatusLine(state, NOW);
        expect(out.dot).toBe('idle');
        expect(out.line).toMatch(/^Unconfirmed · DPC Project · /);
    });
});

describe('an expanded process', () => {
    const names = (id: string | null) => (id === 'group-a' ? 'DPC Project' : id ?? '');
    const labels = (d: { fields: { label: string }[] }) => d.fields.map(f => f.label);
    const value = (d: { fields: { label: string; value: string }[] }, label: string) =>
        d.fields.find(f => f.label === label)?.value;
    const CMD = 'dir /s /b "C:\\Users\\mikha\\Documents\\dpc-messenger\\co"';

    it('carries the command whole and shows only the fields the entry has', () => {
        const d = processDetail(proc({ command: CMD }), names);
        expect(d.command).toBe(CMD);
        expect(d.title).toBe('pid 100 · alive');
        // null exit_reason, stop_kind, exit_code, signal, peak_vram_mib and ended_at are left out, not dashed.
        expect(labels(d)).toEqual([
            'launcher', 'launcher_kind', 'tool', 'round', 'agent_id', 'conversation_id', 'parent_pid', 'started_at',
            'peak_rss_mb', 'peak_commit_mb',
        ]);
        expect(value(d, 'conversation_id')).toBe('DPC Project (group-a)');
        expect(value(d, 'peak_rss_mb')).toBe('512 MB');
    });

    it('says how it ended, with an exit code of 0 and a signal', () => {
        const done = processDetail(proc({ ended_at: iso(5), exit_reason: 'exited', exit_code: 0 }), names);
        expect(value(done, 'exit_reason')).toBe('exited');
        expect(value(done, 'exit_code')).toBe('0');
        expect(done.title).toBe('pid 100 · exited 0');
        const killed = processDetail(proc({ ended_at: iso(5), exit_reason: 'stopped', stop_kind: 'cancelled_by_agent', signal: 'SIGKILL' }), names);
        expect(value(killed, 'stop_kind')).toBe('cancelled_by_agent');
        expect(value(killed, 'signal')).toBe('SIGKILL');
        expect(labels(killed)).not.toContain('exit_code');
    });

    it('spells the fields a newer backend adds', () => {
        const d = processDetail(proc({ cpu_seconds: 12.34, io_read_bytes: 3 * 1024 * 1024, io_write_bytes: 900,
                                       last_output_at: iso(2), peak_vram_mib: 2048 }), names);
        expect(value(d, 'cpu_seconds')).toBe('12 s');
        expect(value(d, 'io_read_bytes')).toBe('3.0 MB');
        expect(value(d, 'io_write_bytes')).toBe('900 B');
        expect(value(d, 'last_output_at')).toBe(iso(2));
        expect(value(d, 'peak_vram_mib')).toBe('2.0 GiB');
        expect(formatBytes(null)).toBe('—');
    });

    it('names the chat only by its id when nothing resolves it', () => {
        expect(value(processDetail(proc({ conversation_id: 'group-z' }), names), 'conversation_id')).toBe('group-z');
        expect(labels(processDetail(proc({ conversation_id: null }), names))).not.toContain('conversation_id');
    });

    it('flags a command as long as the backend keeps, and only that', () => {
        expect(COMMAND_MAX_CHARS).toBe(120);
        expect(processDetail(proc({ command: 'x'.repeat(120) }), names).commandCut).toBe(true);
        expect(processDetail(proc({ command: 'x'.repeat(119) }), names).commandCut).toBe(false);
        expect(COMMAND_CUT_NOTE).toContain('120');
        // No command: the launcher stands, and nothing is said to be cut.
        const bare = processDetail(proc({ command: '' }), names);
        expect(bare.command).toBe('run_shell');
        expect(bare.commandCut).toBe(false);
    });

    it('keeps its key across polls, so an open row stays open', () => {
        const ark = (l: ProcessEntry[]) => build({}, listing(l)).agents.find(a => a.name === 'Ark')!.processes[0];
        expect(processKey(proc({ pid: 9, start_time: 1234.5 }))).toBe('9@1234.5');
        expect(processKey(proc({ pid: 9, start_time: null, started_at: '2026-10-27T10:00:00Z' }))).toBe('9@2026-10-27T10:00:00Z');
        const first = ark([proc({ pid: 9, start_time: 5 })]);
        const later = ark([proc({ pid: 9, start_time: 5, commit_mb: 999, peak_rss_mb: 1 })]);
        expect(later.key).toBe(first.key);
        // A reused pid is another process.
        expect(ark([proc({ pid: 9, start_time: 6 })]).key).not.toBe(first.key);
        expect(first.detail.key).toBe(first.key);
    });

    it('toggles a key in a new set and leaves the old one alone', () => {
        const none = new Set<string>();
        const one = toggleExpanded(none, 'a');
        expect([...one]).toEqual(['a']);
        expect(none.size).toBe(0);
        expect([...toggleExpanded(one, 'b')]).toEqual(['a', 'b']);
        expect(toggleExpanded(one, 'a').size).toBe(0);
    });

    it('the agent row and the system items carry the same detail', () => {
        const sub = { launcher_kind: 'subsystem' as const, agent_id: null, conversation_id: null, tool: null };
        const map = build({}, listing([
            proc({ pid: 9, command: 'pytest -q tests/test_a.py' }),
            proc({ pid: 21, launcher: 'git_helper', ...sub, command: 'git status --porcelain',
                   started_at: iso(50), ended_at: iso(49), exit_reason: 'exited', exit_code: 0 }),
            proc({ pid: 49120, launcher: 'strata', ...sub, command: 'strata serve --port 8080' }),
        ]));
        expect(map.agents.find(a => a.name === 'Ark')!.processes[0].detail.command).toBe('pytest -q tests/test_a.py');
        const live = map.system.find(s => s.name.startsWith('Strata'))!;
        expect(live.details.map(d => d.command)).toEqual(['strata serve --port 8080']);
        const ended = map.system.find(s => s.name.startsWith('Git'))!;
        expect(ended.details.map(d => d.command)).toEqual(['git status --porcelain']);
        expect(live.key).toContain('49120');
        // The finished group's key does not move when another finished helper joins it.
        const more = build({}, listing([
            proc({ pid: 21, launcher: 'git_helper', ...sub, command: 'git status',
                   ended_at: iso(49), exit_reason: 'exited', exit_code: 0 }),
            proc({ pid: 22, launcher: 'git_helper', ...sub, command: 'git diff',
                   ended_at: iso(40), exit_reason: 'exited', exit_code: 0 }),
        ]));
        expect(more.system[0].key).toBe(ended.key);
        expect(more.system[0].details).toHaveLength(2);
    });
});
