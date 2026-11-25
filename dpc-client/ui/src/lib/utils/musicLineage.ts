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
    kind: 'generate' | 'edit';
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
    jobs: Array<{ id: string; type: string; action: string | null; state: string; created_at: string; requested_by: string; take_ids: string[] }>;
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
    for (const k of ['seed', 'lm_seed', 'backbone', 'cot', 'action']) if (p[k] !== undefined) parts.push(`${k} ${String(p[k])}`);
    if (node.kind === 'take') {
        parts.push(qaBadge(node.qa).label, decisionWord(node.decision));
        const why = reasonText(node.reason);
        if (why) parts.push(`reason: ${why}`);
        if (typeof node.lufs_i === 'number') parts.push(`${node.lufs_i.toFixed(1)} LUFS`);
    }
    return parts.join(' · ');
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
