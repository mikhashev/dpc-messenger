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
