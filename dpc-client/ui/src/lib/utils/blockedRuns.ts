// A blocked node's group messages are kept; only their display changes. A run of
// consecutive messages by blocked authors folds into one row that a click opens.
// Pure: no store, no component, so the grouping can be tested on its own.

/** The fields of a group message that name its author node. */
export interface AuthoredMessage {
  /** `sender_node_id` of the record; for an agent's post, still the posting node. */
  sender?: string | null;
  /** `agent_owner`: the node an agent runs on. Empty on a human's message. */
  agentOwner?: string | null;
}

/**
 * True when the message's author node, or the node owning the agent that wrote it,
 * is in `blocked`. A message with neither field (a system line, a pending bubble)
 * is nobody's and is never blocked.
 */
export function isBlockedAuthor(
  blocked: ReadonlySet<string>,
  msg: AuthoredMessage,
): boolean {
  if (blocked.size === 0) return false;
  if (msg.sender && blocked.has(msg.sender)) return true;
  if (msg.agentOwner && blocked.has(msg.agentOwner)) return true;
  return false;
}

export type FoldedRow<T> =
  | { kind: 'message'; message: T; index: number }
  | { kind: 'run'; messages: T[]; startIndex: number };

/**
 * Split `messages` into rows: every message by an author `isBlocked` accepts joins
 * the run it touches, and every other message stays a row of its own. A run may be
 * one message long. Order is the input order; nothing is dropped.
 */
export function foldBlockedRuns<T>(
  messages: readonly T[],
  isBlocked: (message: T) => boolean,
): FoldedRow<T>[] {
  const rows: FoldedRow<T>[] = [];
  let run: { kind: 'run'; messages: T[]; startIndex: number } | null = null;
  messages.forEach((message, index) => {
    if (isBlocked(message)) {
      if (run) {
        run.messages.push(message);
      } else {
        run = { kind: 'run', messages: [message], startIndex: index };
        rows.push(run);
      }
    } else {
      run = null;
      rows.push({ kind: 'message', message, index });
    }
  });
  return rows;
}

/** The text of a folded row: "1 message from a blocked node", "3 messages from a blocked node". */
export function blockedRunLabel(count: number): string {
  return `${count} ${count === 1 ? 'message' : 'messages'} from a blocked node`;
}
