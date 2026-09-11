/**
 * Web Auth Headless Approval Service (ADR-029 Task 008)
 *
 * Backend broadcasts `web_auth_headless_approval_request` before an agent
 * uses stored cookies in a headless browser — the one case where the human
 * cannot see what is being done with their logged-in account, because there
 * is no window. The request waits for an answer only as long as the backend
 * says in `timeout_sec` (120s for headless use, 180s after a login window),
 * and is then refused on the person's behalf.
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
  /** How long the backend waits before it stops listening. Absent on a
   *  backend older than this field, and then the card cannot know. */
  timeout_sec?: number;
  /** Why the backend can no longer act on this request. The card keeps its
   *  place and shows this instead of buttons that resolve to nothing. */
  closed?: string;
}

export const pendingWebAuthApprovals = writable<WebAuthApprovalRequest[]>([]);

/** Retire a card this much *after* the backend's own deadline, never before:
 *  a button greyed out while the backend would still honour it takes a
 *  decision away from the person, and being late costs nothing. */
const EXPIRY_GRACE_MS = 2000;

const expiryTimers = new Map<string, ReturnType<typeof setTimeout>>();

function clearExpiryTimer(requestId: string): void {
  const timer = expiryTimers.get(requestId);
  if (timer !== undefined) {
    clearTimeout(timer);
    expiryTimers.delete(requestId);
  }
}

/**
 * Show a request and arm the clock that says when it is dead.
 *
 * The card has to time itself out because no backend event says so: the only
 * code that learns of the timeout is `_ask_human_to_approve` in `browser.py`,
 * which pops the entry and broadcasts nothing. (The shell gate does have such
 * an emitter — `shell_approval_expired`.) So the deadline is read from the
 * `timeout_sec` the request itself carries.
 */
export function trackWebAuthApproval(request: WebAuthApprovalRequest): void {
  clearExpiryTimer(request.request_id);
  pendingWebAuthApprovals.update((list) => [...list, request]);

  const seconds = Number(request.timeout_sec);
  if (!Number.isFinite(seconds) || seconds <= 0) {
    // A guessed deadline could retire a live card. Leave it live.
    return;
  }

  const timer = setTimeout(() => {
    expiryTimers.delete(request.request_id);
    closeWebAuthApproval(
      request.request_id,
      `No answer within ${Math.round(seconds)}s, so the backend stopped ` +
        `waiting and told the agent no. Nothing was shared. Ask the agent ` +
        `to try again if you meant to allow it.`,
    );
  }, seconds * 1000 + EXPIRY_GRACE_MS);
  expiryTimers.set(request.request_id, timer);
}

/** Keep the card, retire its buttons, and say why. */
export function closeWebAuthApproval(requestId: string, note: string): void {
  clearExpiryTimer(requestId);
  pendingWebAuthApprovals.update((list) =>
    list.map((r) =>
      r.request_id === requestId && !r.closed ? { ...r, closed: note } : r,
    ),
  );
}

/** Take the card off screen: answered, or the notice has been read. */
export function dismissWebAuthApproval(requestId: string): void {
  clearExpiryTimer(requestId);
  pendingWebAuthApprovals.update((list) =>
    list.filter((r) => r.request_id !== requestId),
  );
}
