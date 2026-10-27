// The sidebar's "cached peers" list: what a row says and what its copy buttons put
// on the clipboard. `list_cached_peers` already sorts (newest first) and carries no
// address, so nothing here sorts or filters; it only spells.

/** One row of the `list_cached_peers` answer. */
export interface CachedPeerRow {
  node_id: string;
  display_name?: string | null;
  last_seen?: string | null;
  /** The word `get_status` uses in `peer_info`: a connection to this peer exists now. */
  is_connected?: boolean;
}

/** The rows "Remove all" would remove: every one not connected now. */
export function cachedPeersRemovable(rows: CachedPeerRow[]): CachedPeerRow[] {
  return rows.filter((row) => !row.is_connected);
}

/**
 * The question "Remove all" asks before it removes anything, or '' when there is
 * nothing to remove (every row is connected, or the list is empty).
 */
export function cachedPeersRemoveAllQuestion(rows: CachedPeerRow[]): string {
  const n = cachedPeersRemovable(rows).length;
  if (n === 0) return '';
  return `Remove ${n} ${n === 1 ? 'peer' : 'peers'}? Connected peers stay.`;
}

/** The name a row is shown under; a peer cached without one is said to be unnamed. */
export function cachedPeerName(row: CachedPeerRow): string {
  const name = (row.display_name ?? '').trim();
  return name || '(no name)';
}

/** What the "name + id" button copies: the name and the full node id, one line. */
export function cachedPeerLine(row: CachedPeerRow): string {
  return `${cachedPeerName(row)} ${row.node_id}`;
}

/** What the copy-all button copies: one `cachedPeerLine` per row, in the list's order. */
export function cachedPeerLines(rows: CachedPeerRow[]): string {
  return rows.map(cachedPeerLine).join('\n');
}

/** The date a row was last seen, to the day, or '' when the cache never recorded one. */
export function cachedPeerSeen(row: CachedPeerRow): string {
  if (!row.last_seen) return '';
  const t = Date.parse(row.last_seen);
  if (Number.isNaN(t)) return '';
  return new Date(t).toISOString().slice(0, 10);
}
