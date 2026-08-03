/**
 * Web Auth Headless Approval Service (ADR-029 Task 008)
 *
 * Backend broadcasts `web_auth_headless_approval_request` before an agent
 * uses stored cookies in a headless browser — the one case where the human
 * cannot see what is being done with their logged-in account, because there
 * is no window. The request waits 120s for an answer.
 *
 * Until this service existed nothing listened to that event, so every such
 * request expired: 19 of them across three agents, none ever approved, two
 * minutes of silence each.
 */

import { writable } from "svelte/store";

export interface WebAuthApprovalRequest {
  request_id: string;
  agent_id: string;
  domain: string;
  url: string;
}

export const pendingWebAuthApprovals = writable<WebAuthApprovalRequest[]>([]);
