// Words and arithmetic around Block / Unblock that need no store and no component.

/**
 * The `blocked_nodes` list of a rules object, or null when the object does not carry
 * one. Null is "this payload does not say", not "nobody is blocked": the caller then
 * asks the backend instead of clearing the list. Accepts the rules themselves and the
 * `{rules: ...}` wrapper that `get_firewall_rules` answers with.
 */
export function blockedIdsFromRules(rules: unknown): string[] | null {
  if (!rules || typeof rules !== 'object') return null;
  const body = rules as Record<string, unknown>;
  const direct = body.blocked_nodes;
  const wrapped = (body.rules && typeof body.rules === 'object')
    ? (body.rules as Record<string, unknown>).blocked_nodes
    : undefined;
  const list = Array.isArray(direct) ? direct : Array.isArray(wrapped) ? wrapped : null;
  if (!list) return null;
  return list.filter((id): id is string => typeof id === 'string');
}

/** How many groups list `nodeId` among their members. */
export function groupsWithMember(
  groups: Iterable<{ members?: string[] }>,
  nodeId: string,
): number {
  let n = 0;
  for (const g of groups) {
    if (g.members?.includes(nodeId)) n++;
  }
  return n;
}

export function shortNodeId(nodeId: string): string {
  const prefix = 'dpc-node-';
  return nodeId.startsWith(prefix) ? prefix + nodeId.slice(prefix.length, prefix.length + 8) : nodeId;
}

/**
 * What a peer is called in a sentence: the name part of the sidebar's
 * "name | node_id" display string, else the shortened id.
 */
export function peerLabel(nodeId: string, displayName?: string | null): string {
  const name = (displayName ?? '').split(' | ')[0].trim();
  if (name && name !== nodeId) return name;
  return shortNodeId(nodeId);
}

/** The question the Block confirmation asks. */
export function blockConfirmQuestion(label: string, groupCount: number): string {
  const groups = `${groupCount} ${groupCount === 1 ? 'group' : 'groups'}`;
  return (
    `Block ${label}?\n\n` +
    `Blocking ends the connection, removes ${label} from ${groups}, and deletes ` +
    `every firewall rule that names ${label}. Those removals cannot be undone.\n\n` +
    `Unblock lets ${label} connect again. It does not put them back into groups or rules.`
  );
}

export interface BlockPeerAnswer {
  removed_from_groups?: unknown[];
  removed_rules?: unknown[];
  was_connected?: boolean;
}

/** The notice shown after a successful block, with the counts the backend returned. */
export function blockedNotice(label: string, answer: BlockPeerAnswer): string {
  const groups = answer.removed_from_groups?.length ?? 0;
  const rules = answer.removed_rules?.length ?? 0;
  return (
    `Blocked ${label}. Removed from ${groups} ${groups === 1 ? 'group' : 'groups'}, ` +
    `${rules} firewall ${rules === 1 ? 'rule' : 'rules'} deleted.`
  );
}

/** The notice shown after a successful unblock. */
export function unblockedNotice(label: string): string {
  return `Unblocked ${label}. They can connect again; groups and rules are not restored.`;
}
