/**
 * Fold a group event into the groups store.
 *
 * An event's fields win and the rest of the held entry stays, so an event
 * that omits agents cannot empty the @-list. An event older than the held
 * entry (a lower version) changes nothing, so a late event cannot set an older
 * version beside newer fields. Every group event folded in here carries the
 * version the backend holds after it (`group_deleted` carries none and removes
 * the entry without coming here); an equal version is applied, because the backend has
 * already settled a tie (`apply_sync`) and sends events over one socket in the
 * order it decided them.
 *
 * A group not held is added only from an event that carries its name, creator
 * and roster; a partial event about it is dropped, and `loadGroups` brings the
 * group in whole.
 *
 * The merged entry is not any version's group record (the DPTP object): it is
 * what these events left. The invite event is sent after `apply_sync`, so its
 * `reasoning_effort` and session marker are already this node's own values;
 * `group_updated` comes from this node's own actions or an accepted sync.
 */

type GroupEntry = {
  group_id: string;
  name: string;
  created_by: string;
  members: string[];
  version?: number;
};

export function mergeGroupEvent<T extends GroupEntry>(
  groups: Map<string, T>,
  event: Partial<T> & { group_id: string },
): Map<string, T> {
  const held = groups.get(event.group_id);
  // Present, not truthy: a group with no creator is legal (created_by "").
  if (!held && !('name' in event && 'created_by' in event && 'members' in event)) {
    return groups;
  }
  if (
    held && typeof held.version === 'number' && typeof event.version === 'number'
    && event.version < held.version
  ) {
    return groups;
  }
  const merged = new Map(groups);
  merged.set(event.group_id, { ...held, ...event } as T);
  return merged;
}
