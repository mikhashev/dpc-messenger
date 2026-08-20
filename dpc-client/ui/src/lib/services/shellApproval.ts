/**
 * Shell Approval Service (ADR-030 v2)
 *
 * Manages pending shell command approval requests from agents.
 * Backend broadcasts `shell_approval_request` when an agent tries
 * a Tier 1 command. User approves/rejects via UI buttons.
 */

import { writable } from "svelte/store";

export interface ShellApprovalRequest {
  request_id: string;
  command: string;
  reason: string;
  agent_name: string;
  /** The chat the agent was working in. Empty for runs with no chat behind
   *  them (a schedule, a sleep) and for backends older than this field. */
  conversation_id?: string;
  /** That chat under a name a person recognises — a group name, "Johnny (1:1)",
   *  a peer name, or the id itself when nothing could name it. */
  conversation_title?: string;
}

export interface ShellExecutionResult {
  request_id: string;
  command: string;
  output: string;
  agent_name: string;
  approved_by?: string;
  rejected?: boolean;
}

export const pendingShellApprovals = writable<ShellApprovalRequest[]>([]);
export const shellExecutionResults = writable<ShellExecutionResult[]>([]);
