/**
 * Who may send this node files, as the UI shows it (Mike's call, 2026-10-25).
 *
 * The backend decides in order: the sender's node entry, its first group with an
 * entry, then `file_transfer.proved_connection` for a peer on a direct (proved)
 * connection, and otherwise it asks the user — the offer's event then carries
 * `awaiting_permission: true` and `kind`, whatever the offer is.
 *
 * "Proved" is the peer's key over TLS, not acquaintance: the entry asks by default.
 */

export const PROVED_CONNECTION_KEY = 'proved_connection';

/** Only the direct-connection entry takes `ask`; a node or group entry is allow or deny. */
export const PROVED_CONNECTION_ALLOW_VALUES = ['allow', 'ask', 'deny'] as const;

/** Mirrors firewall.py FILE_TRANSFER_PROVED_CONNECTION_DEFAULT: what applies while the key is absent. */
export const PROVED_CONNECTION_DEFAULT: Readonly<Record<string, any>> = Object.freeze({
    'file_transfer.allow': 'ask',
    'file_transfer.max_size_mb': 1000,
    'file_transfer.allowed_mime_types': ['*'],
});

export interface IncomingOffer {
    transfer_id?: string;
    node_id: string;
    sender_name?: string;
    filename?: string;
    size_bytes?: number;
    group_id?: string;
    awaiting_permission?: boolean;
    kind?: string;
    /** A group file's author as the offer claims it (unsigned); `node_id` is the hop. */
    sender_node_id?: string;
}

/** An asked offer always opens the dialog; otherwise a group file only toasts. */
export function offerOpensDialog(offer: IncomingOffer): boolean {
    return offer.awaiting_permission === true || !offer.group_id;
}

function labelOf(id: string, name: string | undefined): string {
    const shortId = id.length > 20 ? `${id.slice(0, 20)}…` : id;
    const shown = name && name !== id ? name : '';
    return shown ? `${shown} (${shortId})` : shortId || 'an unknown peer';
}

export function senderLabel(offer: IncomingOffer): string {
    return labelOf(offer.node_id || '', offer.sender_name);
}

/** A group file whose claimed author is not the node that sent the offer. */
export function cameThroughAnotherNode(offer: IncomingOffer): boolean {
    return !!offer.group_id && !!offer.sender_node_id && offer.sender_node_id !== offer.node_id;
}

export function authorLabel(offer: IncomingOffer, nameOf?: (nodeId: string) => string | undefined): string {
    const id = offer.sender_node_id || '';
    return labelOf(id, nameOf?.(id));
}

function kindPhrase(kind: string | undefined): string {
    if (kind === 'image') return 'a screenshot';
    if (kind === 'voice') return 'a voice message';
    return 'a file';
}

/** The question for an asked offer, or null for an ordinary one. A group file names its author and the node it came through. */
export function offerQuestion(
    offer: IncomingOffer,
    nameOf?: (nodeId: string) => string | undefined,
): { title: string; detail: string } | null {
    if (offer.awaiting_permission !== true) return null;
    const hop = senderLabel(offer);
    const where = offer.group_id ? ' in a group chat' : '';
    if (cameThroughAnotherNode(offer)) {
        const author = authorLabel(offer, nameOf);
        return {
            title: `Allow files from ${author} via ${hop}?`,
            detail: `${author} sent you ${kindPhrase(offer.kind)}${where}, and it came through ${hop}. `
                + 'Who wrote it is only the offer\'s own claim and is not signed. '
                + `No File Transfer rule names ${hop}, and "Always allow" would remember ${hop}, the node it came through, not ${author}.`,
        };
    }
    return {
        title: `Allow files from ${hop}?`,
        detail: `${hop} wants to send you ${kindPhrase(offer.kind)}${where}. `
            + 'No File Transfer rule names them, so you are asked on their first offer.',
    };
}

/** The limits "Always allow" copies from the direct-connection entry into the node entry it writes. */
export function alwaysAllowLimits(rules: any): { maxSizeMb: number | null; types: string[] } {
    const { entry } = provedConnectionEntry(rules);
    const size = entry['file_transfer.max_size_mb'];
    const types = entry['file_transfer.allowed_mime_types'];
    return {
        maxSizeMb: typeof size === 'number' && size > 0 ? size : null,
        types: Array.isArray(types) && types.length > 0 ? types.map(String) : ['*'],
    };
}

/** The "Always allow" box's text: exactly the entry it writes, and where to revoke it. */
export function alwaysAllowLabel(
    offer: IncomingOffer,
    limits: { maxSizeMb: number | null; types: string[] },
): string {
    const size = limits.maxSizeMb === null ? 'any size' : `up to ${limits.maxSizeMb} MB`;
    const types = limits.types.includes('*') ? 'any type' : limits.types.join(', ');
    const node = cameThroughAnotherNode(offer) ? `${senderLabel(offer)}, the node it came through` : senderLabel(offer);
    return `Always allow files from ${node}: allow, ${size}, ${types}. Revoke in Firewall \u2192 File Transfer.`;
}

/** The entry the File Transfer tab shows for direct peers: the file's own, or the default. */
export function provedConnectionEntry(rules: any): { entry: Record<string, any>; isDefault: boolean } {
    const own = rules?.file_transfer?.[PROVED_CONNECTION_KEY];
    if (own && typeof own === 'object' && !Array.isArray(own)) return { entry: own, isDefault: false };
    return { entry: PROVED_CONNECTION_DEFAULT, isDefault: true };
}

/** Set one key of the direct-peer entry in edited rules, writing the default first if the file had none. */
export function setProvedConnectionRule(rules: any, key: string, value: any): void {
    if (!rules) return;
    if (!rules.file_transfer || typeof rules.file_transfer !== 'object') rules.file_transfer = { groups: {}, nodes: {} };
    const { entry, isDefault } = provedConnectionEntry(rules);
    const target = isDefault ? structuredClone(entry) : entry;
    target[key] = value;
    rules.file_transfer[PROVED_CONNECTION_KEY] = target;
}
