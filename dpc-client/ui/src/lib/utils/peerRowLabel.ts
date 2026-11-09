// The name a connected-peer row shows in the Chats panel. A peer's display name
// can arrive as "Alice (laptop) | dpc-node-0a1b2c3d...": the id belongs in the row's
// title, not in a row that is a few hundred pixels wide.

/** The display name without a trailing " | dpc-node-..." part; the whole text if nothing is left. */
export function peerRowLabel(name: string): string {
  const stripped = name.replace(/\s*\|\s*dpc-node-\S*\s*$/, '').trim();
  return stripped || name;
}
