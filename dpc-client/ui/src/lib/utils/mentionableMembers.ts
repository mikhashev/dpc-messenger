// What the @-completion of a group offers, without a store or a component.

export interface MentionableMember {
  node_id: string;
  name: string;
  mention_name: string;
}

export interface MentionableGroup {
  members?: string[];
  agents?: Record<string, string[]>;
  agent_names?: Record<string, Record<string, string>>;
}

/**
 * The humans and agents a message in `group` can address. `agents` keeps the
 * entries of a node that left (a re-add needs them), so `members` decides whose
 * agents are offered: this node's own included.
 * `localAgentName` is this node's agent list; the group's `agent_names` is the
 * fallback, then the id.
 */
export function mentionableMembers(
  group: MentionableGroup | null | undefined,
  selfId: string,
  peerDisplayNames: Map<string, string>,
  localAgentName: (agentId: string) => string | undefined,
): MentionableMember[] {
  if (!group) return [];
  const members = group.members ?? [];
  const inRoom = new Set(members);
  const result: MentionableMember[] = [];
  for (const nodeId of members) {
    if (nodeId !== selfId) {
      const peerName = peerDisplayNames.get(nodeId)?.split(' | ')[0] || nodeId;
      result.push({ node_id: nodeId, name: peerName, mention_name: peerName });
    }
  }
  const seenAgents = new Set<string>();
  for (const [nodeId, agentIds] of Object.entries(group.agents || {})) {
    if (!inRoom.has(nodeId)) continue;
    const ownerName = nodeId === selfId
      ? 'You'
      : (peerDisplayNames.get(nodeId)?.split(' | ')[0] || nodeId.substring(0, 12));
    for (const agentId of agentIds) {
      if (seenAgents.has(agentId)) continue;
      seenAgents.add(agentId);
      const agentName = localAgentName(agentId) || group.agent_names?.[nodeId]?.[agentId] || agentId;
      result.push({ node_id: agentId, name: `${agentName} (${ownerName})`, mention_name: agentName });
    }
  }
  return result;
}
