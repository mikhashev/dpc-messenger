import { writable } from 'svelte/store';
import { sendCommand } from '$lib/coreService';

export type ScheduleApprovalRequest = {
  request_id: string;
  task_type: string;
  /** Human-readable delay, e.g. "in 1200s" or "immediately". */
  when: string;
  /** What the agent intends to do when it wakes up. */
  about: string;
  conversation_id: string | null;
  agent_name: string;
};

/**
 * Queue requests an agent has made and nobody has answered yet.
 *
 * The decision is taken before the task enters the queue, so what the person
 * sees here is a plan, not something already scheduled behind their back.
 */
export const pendingScheduleApprovals = writable<ScheduleApprovalRequest[]>([]);

function drop(requestId: string) {
  pendingScheduleApprovals.update((list) => list.filter((r) => r.request_id !== requestId));
}

export async function respondToScheduleRequest(requestId: string, approved: boolean) {
  // Drop first: the backend gate is fail-closed and expires on its own, so a
  // failed send must not leave a card the user can press forever.
  drop(requestId);
  await sendCommand('resolve_schedule_approval', { request_id: requestId, approved });
}
