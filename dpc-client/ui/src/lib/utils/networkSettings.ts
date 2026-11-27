/**
 * The Network and Gateway form's guards
 * (THE-NETWORK-HUB-AND-GATEWAY-SETTINGS-HAVE-NO-CONTROL-IN-THE-UI,
 * audit/config_ui_parity_2026-11-27.md): the ports, counts, timeouts and
 * comma lists parse before a command is sent, the way the voice form's do,
 * and the TURN credential fields' placeholder words all the form knows
 * about a stored credential — that it is stored (dots, never the value:
 * the backend answers `credential_set` only).
 */

/** A whole number from `atLeast` to 65535 (a port), or null when the text is not one. */
export function parsePort(text: string | number | undefined | null, atLeast = 1): number | null {
    const n = parseWholeNumber(text, atLeast);
    return n !== null && n <= 65535 ? n : null;
}

/** A whole number `atLeast` or more, or null when the text is not one. */
export function parseWholeNumber(text: string | number | undefined | null, atLeast: number): number | null {
    if (typeof text === 'number') {
        return Number.isSafeInteger(text) && text >= atLeast ? text : null;
    }
    if (typeof text !== 'string') return null;
    const v = text.trim();
    if (!/^-?\d+$/.test(v)) return null;
    const n = Number(v);
    return Number.isSafeInteger(n) && n >= atLeast ? n : null;
}

/** A figure (decimals allowed) strictly above `floor`, or null — a timeout of 0 never resolves. */
export function parseFigureAbove(text: string | number | undefined | null, floor: number): number | null {
    const v = typeof text === 'number' ? String(text) : typeof text === 'string' ? text.trim() : '';
    if (!/^-?\d+(\.\d+)?$/.test(v)) return null;
    const n = Number(v);
    return Number.isFinite(n) && n > floor ? n : null;
}

/** A figure (decimals allowed) `atLeast` or more, or null — 0 is an honest "at once" or "none". */
export function parseFigure(text: string | number | undefined | null, atLeast: number): number | null {
    const v = typeof text === 'number' ? String(text) : typeof text === 'string' ? text.trim() : '';
    if (!/^-?\d+(\.\d+)?$/.test(v)) return null;
    const n = Number(v);
    return Number.isFinite(n) && n >= atLeast ? n : null;
}

/** A comma list of stun:/turn:/turns: addresses as webrtc_peer.py hands them to
 * aiortc (a host with an optional port and query), or null when an entry is
 * not one — the backend refuses the same, these say so first. */
export function parseIceList(text: string | undefined | null): string[] | null {
    const entries = (text ?? '').split(',').map((e) => e.trim()).filter((e) => e.length > 0);
    for (const entry of entries) {
        const match = /^(stun|turn|turns):(\S+)$/i.exec(entry);
        if (!match) return null;
        const host = match[2].split('?')[0];
        if (host.includes(':')) {
            const port = Number(host.split(':').pop());
            if (!Number.isInteger(port) || port < 1 || port > 65535) return null;
        }
    }
    return entries;
}

/** A comma list of host:port entries as get_dht_seed_nodes parses them (the
 * part after the last colon is the port), or null when an entry is not one. */
export function parseHostPortList(text: string | undefined | null): string[] | null {
    const entries = (text ?? '').split(',').map((e) => e.trim()).filter((e) => e.length > 0);
    for (const entry of entries) {
        const idx = entry.lastIndexOf(':');
        if (idx <= 0 || idx === entry.length - 1) return null;
        const port = Number(entry.slice(idx + 1));
        if (!Number.isInteger(port) || port < 1 || port > 65535) return null;
    }
    return entries;
}

/** A checkbox's bool, whatever a command answered. */
export function boolValue(text: unknown, fallback: boolean): boolean {
    return typeof text === 'boolean' ? text : fallback;
}

/** The [p2p] listen host options, the three bind hosts start_server documents. */
export const LISTEN_HOSTS: readonly string[] = ['dual', '0.0.0.0', '::'];

/** The [oauth] default_provider options (the getter's own set: google or github). */
export const OAUTH_PROVIDERS: readonly string[] = ['google', 'github'];

/** `text` when it names one of `allowed`, else `fallback` — a select's value
 * must always be one of its options, whatever a command answered. */
export function allowedValue(text: unknown, allowed: readonly string[], fallback: string): string {
    const value = typeof text === 'string' ? text.trim() : '';
    return allowed.includes(value) ? value : fallback;
}

/** The TURN credential field's placeholder. A stored credential is never sent
 * to the form, so dots stand in for it and the words say what an empty field
 * does (keeps it — only the Remove control clears it). */
export function credentialPlaceholder(set: boolean): string {
    return set
        ? '•••••••• (set — leave empty to keep it)'
        : 'Paste the TURN credential';
}
