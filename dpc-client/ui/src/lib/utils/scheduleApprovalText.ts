/**
 * What the schedule-approval card says, per task type.
 *
 * A `check_back` or other scheduled task is a plan for later: the card says when,
 * what it will do, and asks "Schedule it". A `music_job` (ADR-051 D47) is not: it is
 * a request to use the GPU now, so the card shows the request itself and asks
 * Approve or Refuse. The backend asks through the same gate
 * (`_await_schedule_approval`), so the same event carries both.
 *
 * Fields the card reads: `task_type`, `when`, `about`, and for a `music_job` the
 * structured `music` block, which the backend sends beside `about` (`music_propose_job`,
 * `_approval_parts`). The card builds its sentence from `music`, because `about` can be cut
 * mid-style; `about` stays as the fallback sentence ("Run a music job on the GPU: ...") for a
 * request that carries no block.
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
  /** The length asked for, in seconds. */
  duration_s?: number | null;
  /** The twin of `instrumental`. */
  has_lyrics?: boolean;
  /** The lyrics' first characters; present only when there are lyrics. */
  lyrics_excerpt?: string;
  /** The expected time on the card, in seconds. */
  eta_seconds?: number;
  /** True when `eta_seconds` is a cap-based estimate (YuE2): the card reads it as "up to". */
  eta_is_upper_bound?: boolean;
  /** An edit (`type: "edit"`, ADR-051 row B4): the action, the region in words, the track, whether it holds the GPU. */
  action?: string;
  region_text?: string;
  track_id?: string;
  uses_gpu?: boolean;
  text?: string | null;
}

export const OWNER_APPROVAL_TASK_TYPE = 'owner_approval';
export const KNOWLEDGE_COMMIT_TASK_TYPE = 'knowledge_commit';

/** Who asked, when it was not the owner (ADR-053 2.4): name, node id, human or agent. */
export interface AskerFields {
  name?: string;
  node_id?: string;
  /** "human", "agent" or "unknown" (a peer post whose signature did not verify). */
  kind?: string;
  verified?: boolean;
  /** The own agents that relayed the request, oldest first ("Miron via Ark"). */
  via?: string[];
  /** The backend's own one-line name for the asker, e.g. "Miron via Ark". */
  label?: string;
}

export interface ApprovalRequestLike {
  task_type: string;
  when: string;
  about: string;
  music?: MusicJobRequest | null;
  /** ADR-053: present only when someone other than the owner asked. */
  asker?: AskerFields | null;
  /** ADR-053: the tool, what exactly, and the GPU cost line of an owner card. */
  tool?: string | null;
  details?: string | null;
  cost?: string | null;
}

/** "Miron · human · node dpc-node-1234abcd…", or "" when nobody but the owner asked. */
export function askerLine(asker: AskerFields | null | undefined): string {
  if (!asker) return '';
  const name = (asker.label || asker.name || '').trim() || 'someone unknown';
  const kind = asker.kind === 'agent' ? 'agent' : asker.kind === 'human' ? 'human' : 'not verified';
  const node = (asker.node_id || '').trim();
  const parts = [name, kind];
  if (node) parts.push(`node ${node.length > 24 ? node.slice(0, 24) + '…' : node}`);
  return parts.join(' · ');
}

/** The rows ADR-053 adds to any card: who asked, the tool, what exactly, the cost. */
export function ownerCardRows(request: ApprovalRequestLike, includeTool = true): ApprovalRow[] {
  const rows: ApprovalRow[] = [];
  const who = askerLine(request.asker);
  if (who) rows.push({ label: 'Asked by', value: who });
  if (includeTool && request.tool) rows.push({ label: 'Tool', value: request.tool });
  if (includeTool && request.details) rows.push({ label: 'What exactly', value: request.details, long: true });
  if (request.cost) rows.push({ label: 'Cost', value: request.cost, long: true });
  return rows;
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
  if (m && m.type === 'edit' && typeof m.region_text === 'string' && m.region_text.trim()) {
    const takes = typeof m.variants === 'number' && m.variants > 0 ? `, ${m.variants} ${m.variants === 1 ? 'take' : 'takes'}` : '';
    const what = typeof m.text === 'string' && m.text.trim() ? ` ("${m.text.trim()}")` : '';
    const where = m.track_id ? ` of track ${m.track_id}` : '';
    return `Edit a region${where}: ${m.region_text.trim()}${what}${takes}, ` +
      (m.uses_gpu ? 'on the GPU' : 'no GPU') + '; each take is a new file, the track stays as it is';
  }
  if (m && typeof m.type === 'string' && m.type.trim()) parts.push(humanType(m.type));
  if (m && typeof m.style === 'string' && m.style.trim()) parts.push(m.style.trim());
  if (m && typeof m.variants === 'number' && Number.isFinite(m.variants) && m.variants > 0) {
    parts.push(`${m.variants} ${m.variants === 1 ? 'take' : 'takes'}`);
  }
  if (m && typeof m.instrumental === 'boolean') parts.push(m.instrumental ? 'instrumental' : 'with lyrics');
  if (m && typeof m.engine === 'string' && m.engine.trim()) parts.push(`${m.engine.trim()} engine`);
  else if (m && m.engine === null && parts.length > 0) parts.push('default engine');
  if (m && typeof m.duration_s === 'number' && Number.isFinite(m.duration_s) && m.duration_s > 0) {
    parts.push(`${formatEta(m.duration_s)} long`);
  }
  const eta = formatEta(m?.eta_seconds);
  if (eta) parts.push(`${m?.eta_is_upper_bound === true || m?.engine === 'yue2' ? 'up to' : 'about'} ${eta}`);
  if (parts.length > 0) return `Run a music job on the GPU: ${parts.join(', ')}`;
  const about = (request.about || '').trim();
  return about || 'Run a music job on the GPU';
}

/** The lyrics excerpt row, only when core sent one. */
function lyricsRow(m: MusicJobRequest | null | undefined): ApprovalRow[] {
  const text = m && typeof m.lyrics_excerpt === 'string' ? m.lyrics_excerpt.trim() : '';
  return text ? [{ label: 'Lyrics', value: text, long: true }] : [];
}

export function approvalView(request: ApprovalRequestLike): ApprovalView {
  if (request.task_type === OWNER_APPROVAL_TASK_TYPE) {
    // ADR-053: a flagged tool someone other than the owner asked for.
    const tool = request.tool || 'a tool';
    return {
      isMusicJob: false,
      ariaLabel: `Agent asks to run ${tool} for someone who is not the owner`,
      headText: `asks to run ${tool} for someone who is not the owner`,
      rows: ownerCardRows(request),
      approveLabel: 'Approve',
      refuseLabel: 'Refuse',
    };
  }
  if (request.task_type === KNOWLEDGE_COMMIT_TASK_TYPE) {
    return {
      isMusicJob: false,
      ariaLabel: 'A peer asks to apply a knowledge commit this node did not vote on',
      headText: 'asks to apply a knowledge commit this node did not vote on',
      rows: [
        ...ownerCardRows(request, false),
        { label: 'Commit', value: request.details || request.about || '(no description given)', long: true },
      ],
      approveLabel: 'Apply',
      refuseLabel: 'Refuse',
    };
  }
  if (request.task_type === MUSIC_JOB_TASK_TYPE) {
    return {
      isMusicJob: true,
      ariaLabel: 'Agent asks to run a music job on the GPU',
      headText: 'asks to run a music job',
      rows: [
        ...ownerCardRows(request, false).filter((r) => r.label === 'Asked by'),
        { label: 'Request', value: musicJobSentence(request), long: true },
        ...lyricsRow(request.music),
        ...ownerCardRows(request, false).filter((r) => r.label === 'Cost'),
      ],
      approveLabel: 'Approve',
      refuseLabel: 'Refuse',
    };
  }
  return {
    isMusicJob: false,
    ariaLabel: 'Agent wants to schedule a task',
    headText: 'wants to come back to this later',
    rows: [
      ...ownerCardRows(request, false),
      { label: 'When', value: request.when },
      { label: 'What it will do', value: request.about || '(no description given)', long: true },
    ],
    approveLabel: 'Schedule it',
    refuseLabel: 'Not now',
  };
}
