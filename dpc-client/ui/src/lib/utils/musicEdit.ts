// The pure parts of a region edit proposed from the player (ADR-051 row B4; core music/region_edit.py):
// which edit actions the capability report lets run, the form's checks (the core checks them again),
// the params of `propose_music_job {type: "edit"}`, and the words a job row and the review use.

export type EditAction = 'regenerate_region' | 'fix_seam' | 'remove_region';

export const BUILT_EDIT_ACTIONS: readonly EditAction[] = ['regenerate_region', 'fix_seam', 'remove_region'];
export const REPAINT_ACTIONS: readonly EditAction[] = ['regenerate_region', 'fix_seam'];

export interface ActionStatus {
    status: string;
    reason: string;
}

export interface EditLimits {
    max_region_s: number;
    cut_with_shift_near_end_s: number;
    takes: Record<string, number>;
}

/** The limits the core reports (`limits.edit`), with SPEC §7's values when it says nothing. */
export function editLimits(caps: { limits?: Record<string, any> } | null | undefined): EditLimits {
    const e = caps?.limits?.edit ?? {};
    const num = (v: unknown, d: number) => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : d);
    return {
        max_region_s: num(e.max_region_s, 30),
        cut_with_shift_near_end_s: num(e.cut_with_shift_near_end_s, 10),
        takes: { regenerate_region: 2, fix_seam: 2, remove_region: 1, ...(e.takes ?? {}) },
    };
}

export function isEditAction(action: string | null | undefined): action is EditAction {
    return BUILT_EDIT_ACTIONS.includes(action as EditAction);
}

/** `{supported, reason}` of an edit action from the capability report's `actions`. A report that is
 *  not read yet, or that does not name the action, is not support: the core is the authority. */
export function editActionState(caps: { actions?: Record<string, ActionStatus> } | null | undefined,
                                action: string): { supported: boolean; reason: string } {
    const st = caps?.actions?.[action];
    if (!st) return { supported: false, reason: caps ? 'the backend does not report this action' : 'reading what the backend can do…' };
    return { supported: st.status === 'supported', reason: st.reason };
}

function clock(t: number): string {
    const s = Math.max(0, t);
    const m = Math.floor(s / 60);
    const rest = s - m * 60;
    return `${m}:${rest.toFixed(1).padStart(4, '0')}`;
}

/** "regenerate 0:02.0-0:04.0": the region named as the core names it (`region_edit.region_words`). */
export function regionWords(action: string, region: { t_start: number; t_end: number } | null | undefined): string {
    const verb: Record<string, string> = { regenerate_region: 'regenerate', fix_seam: 'fix seam', remove_region: 'remove' };
    if (!region) return verb[action] ?? action;
    return `${verb[action] ?? action} ${clock(region.t_start)}-${clock(region.t_end)}`;
}

/** Whether a removal ending at `end` may cut with shift (decision 27): it must end near the end. */
export function canCutWithShift(end: number | null, duration: number | null | undefined, nearEnd: number): boolean {
    if (end == null || duration == null || !Number.isFinite(duration) || duration <= 0) return false;
    return end >= duration - nearEnd;
}

export interface EditFormValues {
    action: string;
    start_s: number;
    end_s: number | null;
    text: string;
    /** The track's style, sent as the repaint's caption; empty = the core finds it in the track's record. */
    caption?: string;
    cut_with_shift?: boolean;
}

export interface EditProposal {
    action: EditAction;
    region: { t_start: number; t_end: number };
    prompt: string;
    params: Record<string, unknown>;
}

/** The proposal for `propose_music_job {type: "edit"}`, or the line to show. */
export function editProposal(v: EditFormValues, duration: number | null | undefined, limits: EditLimits):
    { error: string | null; value?: EditProposal } {
    if (!isEditAction(v.action)) return { error: 'Pick an edit action (Regenerate region, Fix seam or Remove region).' };
    if (v.end_s == null) return { error: 'An edit needs a region: set End (drag across the waveform, or R).' };
    const len = v.end_s - v.start_s;
    if (!(len > 0)) return { error: 'End must be greater than Start.' };
    if (len > limits.max_region_s + 1e-9) {
        return { error: `The region is ${len.toFixed(1)} s; an edit takes at most ${limits.max_region_s} s.` };
    }
    const params: Record<string, unknown> = {};
    const caption = (v.caption ?? '').trim();
    if (caption && REPAINT_ACTIONS.includes(v.action)) params.caption = caption;
    if (v.cut_with_shift) {
        if (v.action !== 'remove_region') return { error: 'Cut with shift is for Remove region only.' };
        if (!canCutWithShift(v.end_s, duration, limits.cut_with_shift_near_end_s)) {
            return { error: `Cut with shift is for a region that ends within ${limits.cut_with_shift_near_end_s} s of the end.` };
        }
        params.cut_with_shift = true;
    }
    return {
        error: null,
        value: { action: v.action, region: { t_start: v.start_s, t_end: v.end_s }, prompt: v.text, params },
    };
}

export function buildEditRequest(
    trackId: string,
    proposal: EditProposal,
    title?: string,
): { surface: 'player'; title?: string; form: Record<string, unknown> } {
    const cleanTitle = (title ?? '').replace(/\s+/g, ' ').trim();
    const form: Record<string, unknown> = {
        track_id: trackId,
        action: proposal.action,
        region: { ...proposal.region },
    };
    if (typeof proposal.params?.caption === 'string' && proposal.params.caption.trim()) {
        form.caption = proposal.params.caption.trim();
    }
    if (proposal.params?.cut_with_shift) form.cut_with_shift = true;
    return {
        surface: 'player',
        ...(cleanTitle ? { title: cleanTitle } : {}),
        form,
    };
}

/** The meta line of an edit job's row: what, where, how many takes. */
export function editJobLine(job: { action?: string | null; region?: { t_start: number; t_end: number } | null;
                                    params?: Record<string, any> }): string {
    const takes = job.params?.takes;
    const parts = [`Region edit: ${regionWords(job.action ?? '', job.region ?? null)}`];
    if (typeof takes === 'number') parts.push(`${takes} ${takes === 1 ? 'take' : 'takes'}`);
    if (job.params?.cut_with_shift) parts.push('cut with shift');
    if (job.params?.seed != null) parts.push(`seed ${job.params.seed}`);
    return parts.join(' · ');
}
