import { writable } from 'svelte/store';
import { sendCommand } from '$lib/coreService';
import { scheduleCardRetirement } from '$lib/utils/scheduleApprovalCard';

export type ScheduleApprovalRequest = {
  request_id: string;
  task_type: string;
  /** Human-readable delay, e.g. "in 1200s" or "immediately". */
  when: string;
  /** What the agent intends to do when it wakes up. */
  about: string;
  /** The task being scheduled — an id, not a chat. It used to arrive under
   *  `conversation_id`, which is why the card could never name the chat. */
  task_id?: string | null;
  /** The chat the agent was working in; empty for a run with none behind it. */
  conversation_id: string | null;
  /** That chat under a name a person recognises — a group name, "Johnny (1:1)",
   *  a peer name, or the id itself when nothing could name it. */
  conversation_title?: string;
  agent_name: string;
  /** How long the agent will wait. The card had no deadline to retire on,
   *  so it stayed on screen long after the gate had given up — pressing it
   *  then reported nothing and scheduled nothing. Absent on a backend older
   *  than 2026-08-29, and then the card behaves as it always did. */
  timeout_seconds?: number;
};

/**
 * Queue requests an agent has made and nobody has answered yet.
 *
 * The decision is taken before the task enters the queue, so what the person
 * sees here is a plan, not something already scheduled behind their back.
 */
export const pendingScheduleApprovals = writable<ScheduleApprovalRequest[]>([]);

export function dropScheduleApproval(requestId: string) {
  pendingScheduleApprovals.update((list) => list.filter((r) => r.request_id !== requestId));
}

const drop = dropScheduleApproval;

/**
 * Show a request, and take it away when the agent stops waiting for it.
 *
 * The timer is the whole point: the backend gate expires on its own after
 * `timeout_seconds` and answers nobody afterwards, so a card that outlives it
 * can only mislead — the press reaches a request id the backend no longer
 * holds, and the front end drops the card before reading the refusal.
 *
 * `schedule` is a parameter so a test can watch the expiry without waiting a
 * minute for it.
 */
export function noteScheduleApproval(
  request: ScheduleApprovalRequest,
  schedule: (fn: () => void, ms: number) => unknown = setTimeout,
) {
  pendingScheduleApprovals.update((list) => [...list, request]);
  scheduleCardRetirement(request, dropScheduleApproval, schedule);
}

export async function respondToScheduleRequest(requestId: string, approved: boolean) {
  // Drop first: the backend gate is fail-closed and expires on its own, so a
  // failed send must not leave a card the user can press forever.
  drop(requestId);
  await sendCommand('resolve_schedule_approval', { request_id: requestId, approved });
}
