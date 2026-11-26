// The song lineage graph of the Music Player (core: dpc_client_core/music/lineage.py, `mp.lineage/1`;
// EVOLUTION.md, ADR-051 D29). Pure helpers for the Evolution view (components/music/MusicEvolution.svelte):
// the document types, the branch layout, the edge paths and the words a node and an edge show.
//
// `layoutLineage` is the same algorithm as the core's `lineage.layout`, so this view and the static
// `lineage.html` the core can write place a node alike: a column per generation, leaves in depth-first
// order down the rows, a parent centred on its children.

export const LINEAGE_SCHEMA = 'mp.lineage/1';

export const NODE_W = 190;
export const NODE_H = 62;
export const COL_W = 230;
export const ROW_H = 96;
export const PAD = 24;

export interface LineageReason {
    text: string | null;
    class: string | null;
}

export interface LineageQa {
    verdict: string | null;
    failed: string[];
    not_measured: string[];
    text: string;
}

export type LineageNodeKind = 'intent' | 'source' | 'take';
export type LineageDecision = 'pending' | 'keep' | 'discard';

export interface LineageRequestedBy {
    kind: 'person' | 'agent' | string;
    agent_id?: string;
    agent_name?: string;
    conversation_id?: string;
    conversation_name?: string;
    turn_id?: string;
    surface?: string;
    label?: string;
}

export interface LineageRequestBlock {
    title?: string;
    text?: string;
    asked?: unknown;
    changes?: unknown;
    surface?: string;
    message_id?: string;
    [key: string]: unknown;
}

export interface LineageAdapter {
    name: string;
    music?: number;
    sound?: number;
    strength?: number;
    scale?: number;
}

export interface LineageJob {
    id: string;
    type: string;
    action: string | null;
    state: string;
    created_at: string;
    requested_by: string | LineageRequestedBy;
    request?: LineageRequestBlock | null;
    take_ids: string[];
}

export interface LineageChatNames {
    groups?: Map<string, { name?: string }> | null;
    agents?: Array<{ agent_id: string; name?: string }> | null;
    peers?: Array<{ node_id: string; name?: string }> | null;
}

export interface LineageNode {
    id: string;
    kind: LineageNodeKind;
    label: string;
    generation: number;
    created_at?: string | null;
    job_id?: string;
    engine?: string | null;
    params?: Record<string, unknown>;
    // intent
    job_state?: string | null;
    state_reason?: string | null;
    requested_by?: string | LineageRequestedBy;
    request?: LineageRequestBlock | null;
    // source
    library_track_id?: string | null;
    title?: string;
    // take
    variant?: number | null;
    take_kind?: 'new_track' | 'edit';
    qa?: LineageQa;
    decision?: LineageDecision;
    reason?: LineageReason | null;
    decided_at?: string | null;
    content_hash?: string | null;
    duration_s?: number | null;
    lufs_i?: number | null;
    simulated?: boolean;
    on_kept_path?: boolean;
    ids?: Record<string, string> | null;
}

export interface LineageEdge {
    id: string;
    from: string;
    to: string;
    /** derive: the source track to a song job's intent (op: "cover of", "same style as", ...). */
    kind: 'generate' | 'edit' | 'derive';
    op: string;
    job_id: string;
    group: string;
    region: { t_start: number; t_end: number } | null;
    rejected: boolean;
    reason: LineageReason | null;
}

export interface LineageEvent {
    id: string;
    at: string | null;
    kind: 'keep' | 'discard';
    take_id: string;
    job_id: string;
    over_take_ids: string[];
    ab: boolean;
    reason: LineageReason | null;
    decided_by: { kind: 'person' };
}

export interface MusicLineage {
    schema: string;
    generated_at: string;
    root: { track_id: string | null; job_id: string | null; node_id: string | null };
    nodes: LineageNode[];
    edges: LineageEdge[];
    events: LineageEvent[];
    jobs: LineageJob[];
    stats: { jobs: number; takes: number; kept: number; discarded: number; pending: number; edits: number; branches: number; max_generation: number };
    notes: string[];
}

/** Whether `x` has the shape of an `mp.lineage/1` the view can draw (the core validated it; this guards a stale or foreign answer). */
export function isLineage(x: unknown): x is MusicLineage {
    if (!x || typeof x !== 'object') return false;
    const d = x as Record<string, unknown>;
    return d.schema === LINEAGE_SCHEMA && Array.isArray(d.nodes) && Array.isArray(d.edges) && Array.isArray(d.events)
        && !!d.stats && typeof d.stats === 'object';
}

/** A graph with nothing to show beyond the track itself: no take, so no evolution has been recorded. */
export function hasEvolution(doc: MusicLineage): boolean {
    return doc.nodes.some(n => n.kind === 'take');
}

/** The note the core adds for each edge it left out to break a loop in the stored records. */
export const CYCLE_NOTE = 'Cycle broken:';

/** `{warnings, other}`: the core's notes split into the broken-cycle ones (shown as a warning:
 *  part of the graph is missing) and the rest (the card's quiet footnote). */
export function splitNotes(doc: Pick<MusicLineage, 'notes'>): { warnings: string[]; other: string[] } {
    const notes = Array.isArray(doc.notes) ? doc.notes : [];
    return {
        warnings: notes.filter(n => n.startsWith(CYCLE_NOTE)),
        other: notes.filter(n => !n.startsWith(CYCLE_NOTE)),
    };
}

export interface LineageLayout {
    positions: Record<string, { x: number; y: number }>;
    width: number;
    height: number;
}

export function layoutLineage(doc: Pick<MusicLineage, 'nodes' | 'edges'>): LineageLayout {
    const order = new Map<string, number>(doc.nodes.map((n, i) => [n.id, i]));
    const children = new Map<string, string[]>();
    const hasParent = new Set<string>();
    for (const e of doc.edges) {
        if (!order.has(e.from) || !order.has(e.to)) continue;
        (children.get(e.from) ?? children.set(e.from, []).get(e.from)!).push(e.to);
        hasParent.add(e.to);
    }
    for (const kids of children.values()) kids.sort((a, b) => order.get(a)! - order.get(b)!);
    const rows = new Map<string, number>();
    let nextRow = 0;
    const place = (id: string): number => {
        const known = rows.get(id);
        if (known !== undefined) return known;
        const kids = children.get(id) ?? [];
        if (!kids.length) {
            rows.set(id, nextRow++);
        } else {
            rows.set(id, -1);   // a cycle guard: the core never sends one
            const ys = kids.map(place);
            rows.set(id, ys.reduce((a, b) => a + b, 0) / ys.length);
        }
        return rows.get(id)!;
    };
    for (const n of doc.nodes) if (!hasParent.has(n.id)) place(n.id);
    for (const n of doc.nodes) place(n.id);
    const positions: LineageLayout['positions'] = {};
    let width = 0;
    let height = 0;
    for (const n of doc.nodes) {
        const x = PAD + n.generation * COL_W;
        const y = PAD + (rows.get(n.id) ?? 0) * ROW_H;
        positions[n.id] = { x, y };
        width = Math.max(width, x + NODE_W + PAD);
        height = Math.max(height, y + NODE_H + PAD);
    }
    return { positions, width, height };
}

/** The curve of an edge from the right side of its parent to the left side of its child. */
export function edgePath(from: { x: number; y: number }, to: { x: number; y: number }): string {
    const x1 = from.x + NODE_W;
    const y1 = from.y + NODE_H / 2;
    const x2 = to.x;
    const y2 = to.y + NODE_H / 2;
    const mid = (x1 + x2) / 2;
    return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
}

/** Where an edge's label sits: the middle of its curve, a little above. */
export function edgeLabelAt(from: { x: number; y: number }, to: { x: number; y: number }): { x: number; y: number } {
    return { x: (from.x + NODE_W + to.x) / 2, y: (from.y + to.y + NODE_H) / 2 - 6 };
}

function secondsText(s: number): string {
    return Number.isFinite(s) ? s.toFixed(1) : '?';
}

export function regionText(region: LineageEdge['region']): string {
    return region ? `${secondsText(region.t_start)}-${secondsText(region.t_end)} s` : '';
}

/** "A reason": the person's words, then the failure class when there is one. */
export function reasonText(reason: LineageReason | null | undefined): string {
    if (!reason) return '';
    return [reason.text, reason.class].filter((s): s is string => !!s).join(' / ');
}

const OP_WORDS: Record<string, string> = {
    generate_from_text: 'generated',
    generate_similar: 'generated (same style)',
    cover: 'generated (cover)',
    generate_from_lyrics: 'generated (from lyrics)',
    regenerate_region: 'repaint',
    fix_seam: 'fix seam',
    remove_region: 'remove region',
    extend_ending: 'extend ending',
};

/** The label of an edge: what was done and where; a rejected edge adds the reason, so a dead branch says why. */
export function edgeLabelText(edge: LineageEdge): string {
    const op = OP_WORDS[edge.op] ?? edge.op.replace(/_/g, ' ');
    const region = regionText(edge.region);
    const base = region ? `${op} ${region}` : op;
    const why = edge.rejected ? reasonText(edge.reason) : '';
    return why ? `${base} · rejected: ${why}` : base;
}

export function clip(text: string, max: number): string {
    return text.length <= max ? text : text.slice(0, Math.max(0, max - 1)).trimEnd() + '…';
}

export type NodeTone = 'keep' | 'discard' | 'pending' | 'intent' | 'source';

export function nodeTone(node: LineageNode): NodeTone {
    if (node.kind === 'intent') return 'intent';
    if (node.kind === 'source') return 'source';
    return node.decision === 'keep' ? 'keep' : node.decision === 'discard' ? 'discard' : 'pending';
}

export function decisionWord(decision: LineageDecision | undefined): string {
    return decision === 'keep' ? 'kept' : decision === 'discard' ? 'discarded' : 'pending';
}

/** The QA badge of a take: passed, failed with the gates by name, or not measured (never shown as a pass). */
export function qaBadge(qa: LineageQa | undefined): { kind: 'passed' | 'failed' | 'unknown'; label: string } {
    if (!qa || qa.verdict === 'not_measured' || !qa.verdict) return { kind: 'unknown', label: 'QA not measured' };
    if (qa.verdict === 'failed') return { kind: 'failed', label: qa.failed.length ? `QA failed: ${qa.failed.join(', ')}` : 'QA failed' };
    return { kind: 'passed', label: 'QA passed' };
}

/** The three lines drawn in a node. */
export function nodeLines(node: LineageNode): string[] {
    if (node.kind === 'source') return [clip(node.label, 26), 'source file', ''];
    if (node.kind === 'intent') {
        return [clip(node.label, 26), node.engine ?? '', `intent · ${node.job_state ?? ''}`.trim()];
    }
    const engine = [node.engine, node.variant ? `v${node.variant}` : null, node.take_kind === 'edit' ? 'edit' : null]
        .filter((s): s is string => !!s).join(' · ');
    return [clip(node.label, 26), engine, `${qaBadge(node.qa).label} · ${decisionWord(node.decision)}`];
}

/** The hover text of a node: the facts a person weighs it by. */
export function nodeTitle(node: LineageNode): string {
    const parts = [node.label];
    if (node.engine) parts.push(`engine ${node.engine}`);
    const p = node.params ?? {};
    for (const k of ['seed', 'lm_seed', 'backbone', 'cot', 'action']) {
        if (p[k] !== undefined && p[k] !== null) parts.push(`${k} ${String(p[k])}`);
    }
    if (node.kind === 'take') {
        parts.push(qaBadge(node.qa).label, decisionWord(node.decision));
        const why = reasonText(node.reason);
        if (why) parts.push(`reason: ${why}`);
        if (typeof node.lufs_i === 'number') parts.push(`${node.lufs_i.toFixed(1)} LUFS`);
    }
    return parts.join(' · ');
}

function countSungLines(text: string): number {
    return text.split(/\r?\n/).filter(ln => {
        const s = ln.trim();
        return s.length > 0 && !s.startsWith('[');
    }).length;
}

/** Extract the full multiline lyrics text for the live view's expander, or null for an instrumental. */
export function nodeLyricsText(node: Pick<LineageNode, 'params'>): string | null {
    const p = node.params;
    if (!p) return null;
    if (typeof p.lyrics_text === 'string' && p.lyrics_text.trim()) return p.lyrics_text;
    if (typeof p.lyrics === 'string' && p.lyrics.trim()) return p.lyrics;
    return null;
}

/** Short human summary of a node's lyrics: "instrumental", "34 lines", or null when absent. */
export function lyricsSummary(node: Pick<LineageNode, 'params'>): string | null {
    const p = node.params;
    if (!p) return null;
    const raw = p.lyrics;
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
        const shape = raw as { instrumental?: unknown; lines?: unknown };
        if (shape.instrumental === true) return 'instrumental';
        if (typeof shape.lines === 'number' && Number.isFinite(shape.lines)) {
            return `${shape.lines} ${shape.lines === 1 ? 'line' : 'lines'}`;
        }
    }
    const text = nodeLyricsText(node);
    if (text !== null) {
        const count = countSungLines(text);
        return `${count} ${count === 1 ? 'line' : 'lines'}`;
    }
    if (raw === null) return 'instrumental';
    return null;
}

/** Format one adapter entry with its strengths, e.g. "voice-ru-m-bass-82 (music 0.45, sound 0.35)". */
export function formatAdapter(adapter: unknown): string {
    if (typeof adapter === 'string') return adapter;
    if (!adapter || typeof adapter !== 'object') return '';
    const a = adapter as Record<string, unknown>;
    if (typeof a.name !== 'string' || !a.name) return '';
    const strengths: string[] = [];
    for (const k of ['music', 'sound', 'strength', 'scale']) {
        const v = a[k];
        if (typeof v === 'number' && Number.isFinite(v)) strengths.push(`${k} ${v}`);
    }
    return strengths.length ? `${a.name} (${strengths.join(', ')})` : a.name;
}

/** Format an adapters array (strings or {name, music, sound, strength, scale}) cleanly. */
export function formatAdapters(adapters: unknown): string {
    if (Array.isArray(adapters)) {
        return adapters.map(formatAdapter).filter(Boolean).join(', ');
    }
    return formatAdapter(adapters);
}

/** Format an arbitrary parameter value cleanly without dumping raw JSON. */
function formatParamValue(value: unknown): string {
    if (value === null || value === undefined) return '';
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value);
    if (Array.isArray(value)) return value.map(formatParamValue).filter(Boolean).join(', ');
    if (typeof value === 'object') {
        return Object.entries(value as Record<string, unknown>)
            .filter(([, v]) => v !== null && v !== undefined && v !== '')
            .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${formatParamValue(v)}`)
            .join(', ');
    }
    return String(value);
}

/** Resolve the seed string for an intent node, falling back to its takes when seed is null/omitted. */
export function formatSeed(node: LineageNode, doc?: Pick<MusicLineage, 'nodes'> | null): string | null {
    const raw = node.params?.seed;
    if (typeof raw === 'number' && Number.isFinite(raw)) return String(raw);
    if (typeof raw === 'string' && raw && raw !== 'random') return raw;
    if (node.kind === 'intent' && node.job_id && doc?.nodes) {
        const seeded = doc.nodes
            .filter(n => n.kind === 'take' && n.job_id === node.job_id && typeof n.params?.seed === 'number' && Number.isFinite(n.params.seed))
            .map(n => ({ variant: n.variant, seed: n.params!.seed as number }));
        if (seeded.length === 1) {
            const { variant, seed } = seeded[0];
            return variant != null ? `random (${seed} on v${variant})` : `random (${seed})`;
        }
        if (seeded.length > 1) {
            const unique = new Set(seeded.map(s => s.seed));
            if (unique.size === 1) {
                const labels = seeded.map(s => (s.variant != null ? `v${s.variant}` : '')).filter(Boolean);
                return labels.length === seeded.length
                    ? `random (${seeded[0].seed} on ${labels.join(', ')})`
                    : `random (${seeded[0].seed})`;
            }
            return `random (${seeded.map(s => (s.variant != null ? `v${s.variant}: ${s.seed}` : String(s.seed))).join(', ')})`;
        }
    }
    if (raw === null || raw === 'random') return 'random';
    return null;
}

/** Label/value rows for a node's parameters; never JSON-stringifies objects or dumps raw lyrics_text. */
export function paramRows(node: LineageNode, doc?: Pick<MusicLineage, 'nodes'> | null): Array<[string, string]> {
    const rows: Array<[string, string]> = [];
    const params = node.params ?? {};
    let sawSeed = false;
    for (const [k, v] of Object.entries(params)) {
        if (k === 'lyrics_text') continue;
        if (k === 'seed') {
            sawSeed = true;
            const formatted = formatSeed(node, doc);
            if (formatted) rows.push(['seed', formatted]);
            continue;
        }
        if (v === null || v === undefined) continue;
        if (k === 'lyrics') {
            const summary = lyricsSummary(node);
            if (summary) rows.push(['lyrics', summary]);
            continue;
        }
        if (k === 'adapters') {
            const text = formatAdapters(v);
            if (text) rows.push(['adapters', text]);
            continue;
        }
        const formatted = formatParamValue(v);
        if (formatted) rows.push([k.replace(/_/g, ' '), formatted]);
    }
    if (!sawSeed && node.kind === 'intent') {
        const resolved = formatSeed(node, doc);
        if (resolved) rows.push(['seed', resolved]);
    }
    return rows;
}

function resolveConversationDisplay(id: string | undefined, fallback: string | undefined, names?: LineageChatNames): string | undefined {
    if (!id) return fallback;
    const group = names?.groups?.get(id);
    if (group?.name) return group.name;
    if (id.startsWith('agent_')) {
        const agent = names?.agents?.find(a => a.agent_id === id);
        if (agent?.name && agent.name !== id) return `${agent.name} (1:1)`;
    }
    const peer = names?.peers?.find(p => p.node_id === id);
    if (peer?.name) return peer.name;
    return fallback || id;
}

/** Resolve "asked by Forge in AI Studio" or "asked by you in Player" for a node or job. */
export function formatRequestedBy(
    node: Pick<LineageNode, 'kind' | 'job_id' | 'requested_by' | 'request'>,
    doc?: Pick<MusicLineage, 'jobs'> | null,
    names?: LineageChatNames,
): string {
    if (node.kind === 'source' || !node.job_id) return '';
    const job = doc?.jobs?.find(j => j.id === node.job_id);
    const rb = node.requested_by ?? job?.requested_by;
    const req = node.request ?? job?.request;
    if (!rb || typeof rb === 'string') {
        if (rb === 'agent') return 'asked by agent';
        const surface = req?.surface?.trim() || 'player';
        return `asked by you in ${surface.toLowerCase() === 'player' ? 'Player' : surface}`;
    }
    if (rb.kind === 'agent') {
        const agentMatch = rb.agent_id ? names?.agents?.find(a => a.agent_id === rb.agent_id) : undefined;
        const who = agentMatch?.name || rb.agent_name || rb.agent_id || 'agent';
        const where = resolveConversationDisplay(rb.conversation_id, rb.conversation_name, names);
        return where ? `asked by ${who} in ${where}` : `asked by ${who}`;
    }
    const surface = rb.surface?.trim() || req?.surface?.trim() || 'player';
    return `asked by you in ${surface.toLowerCase() === 'player' ? 'Player' : surface}`;
}

/** Forward-compatible rows for a job's `request` block (Card 7: title, asked, changes, text). */
export function requestRows(
    node: Pick<LineageNode, 'job_id' | 'request'>,
    doc?: Pick<MusicLineage, 'jobs'> | null,
): Array<[string, string]> {
    const job = node.job_id ? doc?.jobs?.find(j => j.id === node.job_id) : undefined;
    const req = node.request ?? job?.request;
    if (!req || typeof req !== 'object') return [];
    const rows: Array<[string, string]> = [];
    const pushIf = (label: string, val: unknown, sep = ', ') => {
        if (val === null || val === undefined || val === '') return;
        const text = Array.isArray(val) ? val.map(formatParamValue).filter(Boolean).join(sep) : formatParamValue(val);
        if (text) rows.push([label, text]);
    };
    pushIf('title', req.title);
    pushIf('asked', req.asked);
    pushIf('changes', req.changes, '; ');
    pushIf('request', req.text);
    const handled = new Set(['title', 'asked', 'changes', 'text', 'surface', 'message_id']);
    for (const [k, v] of Object.entries(req)) {
        if (!handled.has(k)) pushIf(k.replace(/_/g, ' '), v);
    }
    return rows;
}

/** The node that can be played: a take with its library entry. */
export function playableNode(node: LineageNode | null | undefined): string | null {
    return node && node.kind === 'take' && node.library_track_id ? node.library_track_id : null;
}

/** The node to start with: the one the core focused, else the first take. */
export function focusNode(doc: MusicLineage): LineageNode | null {
    const byId = new Map(doc.nodes.map(n => [n.id, n]));
    return (doc.root.node_id ? byId.get(doc.root.node_id) : null) ?? doc.nodes.find(n => n.kind === 'take') ?? doc.nodes[0] ?? null;
}

/** One line per selection, for the list under the graph: "Kept v1 over 1 other take", "Discarded v2: robotic vocals". */
export function eventLine(doc: MusicLineage, ev: LineageEvent): string {
    const node = doc.nodes.find(n => n.id === ev.take_id);
    const name = node ? node.label : ev.take_id;
    if (ev.kind === 'keep') {
        const over = ev.over_take_ids.length ? ` over ${ev.over_take_ids.length} other take${ev.over_take_ids.length === 1 ? '' : 's'}` : '';
        return `Kept ${name}${over}`;
    }
    const why = reasonText(ev.reason);
    return `Discarded ${name}${why ? `: ${why}` : ''}`;
}
