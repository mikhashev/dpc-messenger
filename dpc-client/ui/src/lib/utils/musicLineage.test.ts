import { describe, expect, it } from 'vitest';
import {
    clip,
    decisionWord,
    edgeLabelText,
    edgePath,
    eventLine,
    focusNode,
    hasEvolution,
    isLineage,
    layoutLineage,
    nodeLines,
    nodeTone,
    playableNode,
    qaBadge,
    reasonText,
    type LineageEdge,
    type LineageNode,
    type MusicLineage,
} from './musicLineage';

const qa = (verdict: string, failed: string[] = []) => ({ verdict, failed, not_measured: [], text: `QA ${verdict}` });

function take(id: string, generation: number, over: Partial<LineageNode> = {}): LineageNode {
    return {
        id, kind: 'take', label: `song ${id}`, generation, job_id: 'job_a', variant: 1, take_kind: 'new_track', engine: 'yue2.cpp',
        qa: qa('passed'), decision: 'pending', reason: null, library_track_id: `trk_${id}`, params: {}, ...over,
    };
}

function edge(from: string, to: string, over: Partial<LineageEdge> = {}): LineageEdge {
    return { id: `${from}>${to}`, from, to, kind: 'generate', op: 'generate_from_text', job_id: 'job_a', group: 'job_a', region: null, rejected: false, reason: null, ...over };
}

// The graph the core's test builds (tests/test_the_song_lineage_graph_...py): one intent, two takes, the
// first with two edit siblings. The positions below are what the core's `lineage.layout` answers for it.
const DOC: MusicLineage = {
    schema: 'mp.lineage/1', generated_at: 'now', root: { track_id: 'trk_t1', job_id: null, node_id: 't1' },
    nodes: [
        { id: 'int', kind: 'intent', label: 'indie rock', generation: 0, engine: 'yue2.cpp', job_state: 'accepted' },
        take('t1', 1, { decision: 'keep', on_kept_path: true }),
        take('t2', 1, { decision: 'discard', variant: 2, qa: qa('failed', ['clipping']), reason: { text: 'vocals sound robotic', class: 'opt' } }),
        take('e1', 2, { take_kind: 'edit' }),
        take('e2', 2, { take_kind: 'edit', decision: 'discard', reason: { text: 'seam clicks', class: null } }),
    ],
    edges: [
        edge('int', 't1'),
        edge('int', 't2', { rejected: true, reason: { text: 'vocals sound robotic', class: 'opt' } }),
        edge('t1', 'e1', { kind: 'edit', op: 'regenerate_region', group: 'job_e', region: { t_start: 20, t_end: 30 } }),
        edge('t1', 'e2', { kind: 'edit', op: 'regenerate_region', group: 'job_e', region: { t_start: 20, t_end: 30 }, rejected: true, reason: { text: 'seam clicks', class: null } }),
    ],
    events: [
        { id: 'sel:t1', at: '1', kind: 'keep', take_id: 't1', job_id: 'job_a', over_take_ids: ['t2'], ab: true, reason: null, decided_by: { kind: 'person' } },
        { id: 'sel:t2', at: '2', kind: 'discard', take_id: 't2', job_id: 'job_a', over_take_ids: [], ab: true, reason: { text: 'vocals sound robotic', class: 'opt' }, decided_by: { kind: 'person' } },
    ],
    jobs: [], stats: { jobs: 2, takes: 4, kept: 1, discarded: 2, pending: 1, edits: 2, branches: 3, max_generation: 2 }, notes: [],
};

describe('layoutLineage', () => {
    it('places a column per generation and centres a parent on its children, like the core', () => {
        const { positions, width, height } = layoutLineage(DOC);
        expect(positions.int).toEqual({ x: 24, y: 144 });
        expect(positions.t1).toEqual({ x: 254, y: 72 });
        expect(positions.e1).toEqual({ x: 484, y: 24 });
        expect(positions.e2).toEqual({ x: 484, y: 120 });
        expect(positions.t2).toEqual({ x: 254, y: 216 });
        expect(width).toBe(484 + 190 + 24);
        expect(height).toBe(216 + 62 + 24);
    });

    it('never puts two nodes on the same spot and ignores an edge to a node that is not there', () => {
        const withBad = { nodes: DOC.nodes, edges: [...DOC.edges, edge('t1', 'ghost')] };
        const { positions } = layoutLineage(withBad);
        const spots = Object.values(positions).map(p => `${p.x},${p.y}`);
        expect(new Set(spots).size).toBe(spots.length);
        expect(positions.ghost).toBeUndefined();
    });

    it('gives a lone node the first row', () => {
        const { positions } = layoutLineage({ nodes: [DOC.nodes[0]], edges: [] });
        expect(positions.int).toEqual({ x: 24, y: 24 });
    });
});

describe('edges', () => {
    it('curves from the right of the parent to the left of the child', () => {
        expect(edgePath({ x: 0, y: 0 }, { x: 230, y: 96 })).toBe('M190,31 C210,31 210,127 230,127');
    });

    it('labels what was done and where, and a rejected edge says why', () => {
        expect(edgeLabelText(DOC.edges[0])).toBe('generated');
        expect(edgeLabelText(DOC.edges[1])).toBe('generated · rejected: vocals sound robotic / opt');
        expect(edgeLabelText(DOC.edges[2])).toBe('repaint 20.0-30.0 s');
        expect(edgeLabelText(DOC.edges[3])).toBe('repaint 20.0-30.0 s · rejected: seam clicks');
        expect(edgeLabelText(edge('a', 'b', { op: 'some_new_op' }))).toBe('some new op');
    });
});

describe('what a node shows', () => {
    it('badges QA without ever showing "not measured" as a pass', () => {
        expect(qaBadge(qa('passed'))).toEqual({ kind: 'passed', label: 'QA passed' });
        expect(qaBadge(qa('failed', ['clipping', 'silence']))).toEqual({ kind: 'failed', label: 'QA failed: clipping, silence' });
        expect(qaBadge(qa('not_measured'))).toEqual({ kind: 'unknown', label: 'QA not measured' });
        expect(qaBadge(undefined).kind).toBe('unknown');
    });

    it('tones a node by its decision and kind', () => {
        expect(DOC.nodes.map(nodeTone)).toEqual(['intent', 'keep', 'discard', 'pending', 'discard']);
        expect(nodeTone({ id: 's', kind: 'source', label: 'x', generation: 0 })).toBe('source');
        expect(decisionWord('keep')).toBe('kept');
        expect(decisionWord(undefined)).toBe('pending');
    });

    it('draws engine, edit mark, QA and decision on a take, and clips a long label', () => {
        expect(nodeLines(DOC.nodes[1])).toEqual(['song t1', 'yue2.cpp · v1', 'QA passed · kept']);
        expect(nodeLines(DOC.nodes[3])[1]).toBe('yue2.cpp · v1 · edit');
        expect(nodeLines(DOC.nodes[2])[2]).toBe('QA failed: clipping · discarded');
        expect(nodeLines(DOC.nodes[0])).toEqual(['indie rock', 'yue2.cpp', 'intent · accepted']);
        expect(clip('a'.repeat(40), 10)).toBe('aaaaaaaaa…');
    });

    it('words a reason with its class, and nothing for none', () => {
        expect(reasonText({ text: 'muddy', class: 'sel' })).toBe('muddy / sel');
        expect(reasonText({ text: null, class: 'struct' })).toBe('struct');
        expect(reasonText(null)).toBe('');
    });

    it('plays only a take that has a library entry', () => {
        expect(playableNode(DOC.nodes[1])).toBe('trk_t1');
        expect(playableNode(take('x', 1, { library_track_id: null }))).toBeNull();
        expect(playableNode(DOC.nodes[0])).toBeNull();
        expect(playableNode(null)).toBeNull();
    });
});

describe('the document', () => {
    it('is recognised by its schema and shape, and a graph with no take has no evolution', () => {
        expect(isLineage(DOC)).toBe(true);
        expect(isLineage({ ...DOC, schema: 'mp.lineage/2' })).toBe(false);
        expect(isLineage(null)).toBe(false);
        expect(isLineage({ schema: 'mp.lineage/1', nodes: [] })).toBe(false);
        expect(hasEvolution(DOC)).toBe(true);
        expect(hasEvolution({ ...DOC, nodes: [{ id: 's', kind: 'source', label: 'x', generation: 0 }] })).toBe(false);
    });

    it('starts on the focused node, else the first take', () => {
        expect(focusNode(DOC)?.id).toBe('t1');
        expect(focusNode({ ...DOC, root: { ...DOC.root, node_id: null } })?.id).toBe('t1');
        expect(focusNode({ ...DOC, nodes: [], edges: [] })).toBeNull();
    });

    it('lists the selections in a line each', () => {
        expect(eventLine(DOC, DOC.events[0])).toBe('Kept song t1 over 1 other take');
        expect(eventLine(DOC, DOC.events[1])).toBe('Discarded song t2: vocals sound robotic / opt');
    });
});
