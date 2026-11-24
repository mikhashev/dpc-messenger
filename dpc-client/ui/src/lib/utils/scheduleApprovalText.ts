/**
 * What the schedule-approval card says, per task type.
 *
 * A `check_back` or other scheduled task is a plan for later: the card says when,
 * what it will do, and asks "Schedule it". A `music_job` (ADR-051 D47) is not: it is
 * a request to use the GPU now, so the card shows the request itself and asks
 * Approve or Refuse. The backend asks through the same gate
 * (`_await_schedule_approval`), so the same event carries both.
 *
 * Fields the card reads: `task_type`, `when`, `about` (all sent today). A `music_job`
 * `about` is already a sentence ("Generate music on the GPU: 2 variant(s), ...", cut
 * at 200 characters). When the backend also sends a structured `music` block, the
 * card builds the sentence from it, because `about` can be cut mid-style.
 */

export const MUSIC_JOB_TASK_TYPE = 'music_job';

/** The structured part of a music_job request; every field is optional. */
export interface MusicJobRequest {
  /** The job type, e.g. "generate_from_text". */
  type?: string;
  style?: string;
  variants?: number;
  /** true: no lyrics (an instrumental); false: sung, with lyrics. Absent: not said. */
  instrumental?: boolean;
  /** The engine's name; null or absent means the default engine. */
  engine?: string | null;
  /** The expected time on the card, in seconds. */
  eta_seconds?: number;
}

export interface ApprovalRequestLike {
  task_type: string;
  when: string;
  about: string;
  music?: MusicJobRequest | null;
}

export interface ApprovalRow {
  label: string;
  value: string;
  /** Long free text: drawn in the scrollable block. */
  long?: boolean;
}

export interface ApprovalView {
  isMusicJob: boolean;
  ariaLabel: string;
  /** What follows the agent's name in the card's head. */
  headText: string;
  rows: ApprovalRow[];
  approveLabel: string;
  refuseLabel: string;
}

/** "90 s" under two minutes, "about 5 min" above; '' when unknown. */
export function formatEta(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return '';
  if (seconds < 120) return `${Math.round(seconds)} s`;
  return `${Math.round(seconds / 60)} min`;
}

function humanType(type: string): string {
  return type.trim().replace(/_/g, ' ');
}

/** The one-line request a music_job card shows. */
export function musicJobSentence(request: Pick<ApprovalRequestLike, 'about' | 'music'>): string {
  const m = request.music;
  const parts: string[] = [];
  if (m && typeof m.type === 'string' && m.type.trim()) parts.push(humanType(m.type));
  if (m && typeof m.style === 'string' && m.style.trim()) parts.push(m.style.trim());
  if (m && typeof m.variants === 'number' && Number.isFinite(m.variants) && m.variants > 0) {
    parts.push(`${m.variants} ${m.variants === 1 ? 'take' : 'takes'}`);
  }
  if (m && typeof m.instrumental === 'boolean') parts.push(m.instrumental ? 'instrumental' : 'with lyrics');
  if (m && typeof m.engine === 'string' && m.engine.trim()) parts.push(`${m.engine.trim()} engine`);
  else if (m && m.engine === null && parts.length > 0) parts.push('default engine');
  const eta = formatEta(m?.eta_seconds);
  if (eta) parts.push(`about ${eta}`);
  if (parts.length > 0) return `Run a music job on the GPU: ${parts.join(', ')}`;
  const about = (request.about || '').trim();
  return about || 'Run a music job on the GPU';
}

export function approvalView(request: ApprovalRequestLike): ApprovalView {
  if (request.task_type === MUSIC_JOB_TASK_TYPE) {
    return {
      isMusicJob: true,
      ariaLabel: 'Agent asks to run a music job on the GPU',
      headText: 'asks to run a music job',
      rows: [{ label: 'Request', value: musicJobSentence(request), long: true }],
      approveLabel: 'Approve',
      refuseLabel: 'Refuse',
    };
  }
  return {
    isMusicJob: false,
    ariaLabel: 'Agent wants to schedule a task',
    headText: 'wants to come back to this later',
    rows: [
      { label: 'When', value: request.when },
      { label: 'What it will do', value: request.about || '(no description given)', long: true },
    ],
    approveLabel: 'Schedule it',
    refuseLabel: 'Not now',
  };
}
