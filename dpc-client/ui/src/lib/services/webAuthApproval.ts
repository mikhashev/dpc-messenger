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
  /** Which decision is being asked for: "headless_use" or "login_window". */
  kind?: string;
  /** The question in the backend's own words; the card falls back if absent. */
  question?: string;
  /** Context for the person — never the reason anything is granted. */
  evidence?: {
    baseline_count?: number;
    now_count?: number;
    new_cookie_names?: string[];
  };
  agent_id: string;
  /** The agent under the name it uses in chat; falls back to its id. */
  agent_name?: string;
  domain: string;
  url: string;
  /** The chat the agent was working in; empty for a run with none behind it. */
  conversation_id?: string;
  /** That chat under a name a person recognises. */
  conversation_title?: string;
}

export const pendingWebAuthApprovals = writable<WebAuthApprovalRequest[]>([]);
