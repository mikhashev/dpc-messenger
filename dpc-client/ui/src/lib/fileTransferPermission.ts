/**
 * Who may send this node files, as the UI shows it (Mike's call, 2026-10-25).
 *
 * The backend decides in order: the sender's node entry, its first group with an
 * entry, then `file_transfer.proved_connection` for a peer on a direct (proved)
 * connection, and otherwise it asks the user — the offer's event then carries
 * `awaiting_permission: true` and `kind`, whatever the offer is.
 */

export const PROVED_CONNECTION_KEY = 'proved_connection';

/** Mirrors firewall.py FILE_TRANSFER_PROVED_CONNECTION_DEFAULT: what applies while the key is absent. */
export const PROVED_CONNECTION_DEFAULT: Readonly<Record<string, any>> = Object.freeze({
    'file_transfer.allow': 'allow',
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
}

/** An asked offer always opens the dialog; otherwise a group file only toasts. */
export function offerOpensDialog(offer: IncomingOffer): boolean {
    return offer.awaiting_permission === true || !offer.group_id;
}

export function senderLabel(offer: IncomingOffer): string {
    const id = offer.node_id || '';
    const shortId = id.length > 20 ? `${id.slice(0, 20)}…` : id;
    const name = offer.sender_name && offer.sender_name !== id ? offer.sender_name : '';
    return name ? `${name} (${shortId})` : shortId || 'an unknown peer';
}

function kindPhrase(kind: string | undefined): string {
    if (kind === 'image') return 'a screenshot';
    if (kind === 'voice') return 'a voice message';
    return 'a file';
}

/** The question for an asked offer, or null for an ordinary one. */
export function offerQuestion(offer: IncomingOffer): { title: string; detail: string } | null {
    if (offer.awaiting_permission !== true) return null;
    const who = senderLabel(offer);
    const where = offer.group_id ? ' in a group chat' : '';
    return {
        title: `Allow files from ${who}?`,
        detail: `${who} wants to send you ${kindPhrase(offer.kind)}${where}. `
            + 'They are not on a direct connection with you, and no File Transfer rule names them.',
    };
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
