/**
 * The "Your Telegram user ID (owner)" field of Edit Telegram Configuration: the
 * agent's `telegram_owner_user_id`. Turns from this user in a private chat with the
 * agent's bot count as the owner's and get the agent's memory; an empty field means
 * no Telegram turn does. The backend owns the rule (digits only,
 * `telegram_owner_user_id_from` in dpc_agent/utils.py) and checks it again; this
 * keeps the dialog from sending what it would refuse.
 */

/** What the field shows for an agent: its stored id, or empty when the row has none. */
export function ownerUserIdFromAgent(stored: string | null | undefined): string {
    return typeof stored === 'string' ? stored : '';
}

/**
 * What the field holds, ready to send: digits, or null for an emptied field (no
 * owner). Surrounding spaces are dropped; anything else is an error the dialog
 * can show.
 */
export function ownerUserIdToSend(
    typed: string | null | undefined
): { value: string | null } | { error: string } {
    const text = (typed ?? '').trim();
    if (text === '') {
        return { value: null };
    }
    if (!/^[0-9]+$/.test(text)) {
        return { error: 'Your Telegram user ID must be digits only (the number @userinfobot shows)' };
    }
    return { value: text };
}
