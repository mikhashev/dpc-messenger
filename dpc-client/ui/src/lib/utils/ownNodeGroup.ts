/**
 * The `own_node_group` picker of the agent permissions panel (ADR-053 step 7, ADR-048
 * item 2).
 *
 * An agent's own nodes are the members of one top-level `node_groups` entry, named in the
 * agent profile's `memory.own_node_group`. A request from one of them is the owner's
 * (ADR-053 2.2): its flagged tools run without a card. Being in the group grants no file
 * rights by itself. The save goes through the firewall editor's own save, and the backend
 * (`firewall.validate_config`) refuses a name that is not in `node_groups`.
 */

/** The group names the picker offers: the keys of `node_groups`, without `_comment`-style keys. */
export function ownNodeGroupOptions(nodeGroups: Record<string, unknown> | null | undefined): string[] {
  if (!nodeGroups || typeof nodeGroups !== 'object') return [];
  return Object.keys(nodeGroups).filter((k) => k && !k.startsWith('_')).sort();
}

/**
 * The memory block with the choice applied: a name sets `own_node_group`; "" (none) removes
 * the field, so an agent with no own node group has no key rather than an empty one.
 */
export function withOwnNodeGroup<T extends Record<string, unknown>>(memory: T | null | undefined, name: string): T {
  const next = { ...(memory || {}) } as Record<string, unknown>;
  if (name) next.own_node_group = name;
  else delete next.own_node_group;
  return next as T;
}

/** What the display mode shows for the current value. */
export function ownNodeGroupLabel(memory: Record<string, unknown> | null | undefined): string {
  const name = memory && typeof memory.own_node_group === 'string' ? memory.own_node_group : '';
  return name || 'none (only this node is the owner)';
}
