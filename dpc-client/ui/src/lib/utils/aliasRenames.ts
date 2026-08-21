/**
 * Which alias a reference still carries, after any number of edits in one editing session.
 *
 * References outside providers.json (agent configs, the registry, the firewall, the voice
 * list) hold the name the alias had when editing began, so a chain a→b→c must reach the
 * backend as a→c, and a→b→a as nothing at all.
 */
export function trackRename(
  pending: Record<string, string>,
  oldAlias: string,
  newAlias: string
): Record<string, string> {
  const next = { ...pending };

  if (!oldAlias || !newAlias || oldAlias === newAlias) return next;

  let origin = oldAlias;
  for (const [from, to] of Object.entries(next)) {
    if (to === oldAlias) {
      origin = from;
      delete next[origin];
      break;
    }
  }

  if (origin !== newAlias) next[origin] = newAlias;
  return next;
}
