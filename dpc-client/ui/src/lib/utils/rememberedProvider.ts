/**
 * Which provider choice is worth remembering for a chat.
 *
 * The Text dropdown's value is a `uniqueId` — `local:<alias>` for a provider on
 * this machine, `remote:…` for one served by a peer. The map it is remembered in
 * holds a bare alias, because that is what the query payload's `provider` field
 * expects; putting a `remote:` string there would send the prefix to the backend
 * as if it were an alias.
 *
 * Remote is therefore not remembered here at all: that selection is a
 * compute-host choice with a path of its own, and pretending to store it would
 * be worse than storing nothing.
 *
 * The reason any of this exists: nothing used to store the choice, so the effect
 * that restores a selection on every chat switch had nothing to find and fell to
 * `default_provider` — on the machine where this was observed, a paid alias at
 * max effort. A person who had deliberately moved to a local free model was
 * returned to it without a word, and only in that direction.
 */

const LOCAL_PREFIX = 'local:';

/**
 * The alias to store for this chat, or `null` when nothing should be written.
 *
 * @param uniqueId the dropdown's value
 * @param existing what the map already holds for this chat
 */
export function providerToRemember(
    uniqueId: string | null | undefined,
    existing: string | null | undefined,
): string | null {
    if (!uniqueId || !uniqueId.startsWith(LOCAL_PREFIX)) return null;
    const alias = uniqueId.slice(LOCAL_PREFIX.length);
    if (!alias) return null;
    // Writing the value it already holds would notify every subscriber for
    // nothing — including the effect that reads this map to set the dropdown.
    if (alias === existing) return null;
    return alias;
}
