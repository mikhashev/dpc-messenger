import { describe, expect, it } from 'vitest';
import {
    buildCreateRequest,
    emptyFromTextForm,
    jobTitle,
    type MusicJob,
} from './musicCreate';
import { buildSimilarRequest, emptySimilarForm, validateSimilarForm, type SongSource } from './musicSimilar';
import { buildEditRequest, editLimits, editProposal } from './musicEdit';

describe('Wave 2 Card 7: player jobs attach request block and prefer request.title', () => {
    it('jobTitle prefers request.title over clipped style/prompt', () => {
        const j: MusicJob = {
            id: 'job_0000000000000001',
            type: 'generate_from_text',
            state: 'queued',
            params: { style: 'a very long style prompt that should not show as the title', variants: 1, duration_s: 30 },
            prompt: 'fallback prompt',
            request: { surface: 'player', title: '  Electric   Horizon  ' },
            created_at: '2026-11-26T10:00:00Z',
        };
        expect(jobTitle(j)).toBe('Electric Horizon');
    });

    it('buildCreateRequest shapes player request with surface, optional title and form inputs', () => {
        const form = { ...emptyFromTextForm(), title: '  Neon Rain  ', style: 'synthwave, warm pads', lyrics: '[verse]\nhi', variants: 2, duration: 180, seed: '42' };
        const req = buildCreateRequest(form);
        expect(req).toEqual({
            surface: 'player',
            title: 'Neon Rain',
            form: {
                engine: 'yue2',
                backbone: 'stock',
                style: 'synthwave, warm pads',
                instrumental: false,
                variants: 2,
                duration_s: 180,
                seed: 42,
            },
        });
        const untitled = buildCreateRequest({ ...emptyFromTextForm(), style: 'folk', lyrics: '[verse]\na' });
        expect(untitled.surface).toBe('player');
        expect('title' in untitled).toBe(false);
    });

    it('buildSimilarRequest shapes player request for same_style and cover without changing validateSimilarForm', () => {
        const src: SongSource = { id: 'trk_1', title: 'Night Drive', kind: 'local', durationS: 200, hashState: 'done' };
        const form = { ...emptySimilarForm(src, 'cover'), title: 'Bossa Rework', caption: 'bossa nova', captionEdited: true };
        const validated = validateSimilarForm(form);
        expect(validated.ok).toBe(true);
        if (!validated.ok) return;
        expect(buildSimilarRequest(form, validated.params)).toEqual({
            surface: 'player',
            title: 'Bossa Rework',
            form: {
                mode: 'cover',
                source_track_id: 'trk_1',
                source_title: 'Night Drive',
                engine: 'yue2',
                caption: 'bossa nova',
                lyrics_mode: 'reuse',
                cot: 'melody',
                variants: 2,
            },
        });
    });

    it('buildEditRequest shapes player request for region edits', () => {
        const limits = editLimits(null);
        const proposal = editProposal({ action: 'remove_region', start_s: 10, end_s: 12, text: 'cough', cut_with_shift: false }, 120, limits);
        expect(proposal.value).toBeDefined();
        if (!proposal.value) return;
        expect(buildEditRequest('trk_1', proposal.value)).toEqual({
            surface: 'player',
            form: {
                track_id: 'trk_1',
                action: 'remove_region',
                region: { t_start: 10, t_end: 12 },
            },
        });
    });
});
