// One line per local engine for the Local engines block, from local_engine_status
// `pins` (B2 step 3): the pin this build names, the retained pin kept beside it,
// the pin actually running when it is another, and a warning after a rollback.

export interface EnginePinRollback {
    from: string;
    to: string;
    reason: string;
    at: number;
}

export interface EnginePins {
    pin: string;
    retained_pin: string | null;
    running_pin: string | null;
    rolled_back_to: EnginePinRollback | null;
    installed_pins: { tag: string; platform: string; start_passed_at: string | null }[];
}

export interface EnginePinLine {
    text: string;
    warn: boolean;
}

const UNTIL: Record<string, string> = {
    strata: 'until the alias is Reset or the backend restarts',
};

export function enginePinLines(pins: Record<string, EnginePins> | null | undefined): EnginePinLine[] {
    const lines: EnginePinLine[] = [];
    for (const [name, p] of Object.entries(pins ?? {})) {
        if (!p) continue;
        const currentPinInstalled = (p.installed_pins ?? []).some((i) => i.tag === p.pin);
        if (!currentPinInstalled && !p.retained_pin) continue; // nothing of this engine on disk
        let text = `${name} ${p.pin}`;
        if (p.retained_pin) text += ` (retained ${p.retained_pin})`;
        if (!currentPinInstalled) text += ` — ${p.pin} not downloaded yet`;
        if (p.running_pin && p.running_pin !== p.pin) text += ` — running ${p.running_pin}`;
        lines.push({ text, warn: false });
        const r = p.rolled_back_to;
        if (r) {
            lines.push({
                text: `${name} ${r.from} failed its first start (${r.reason}); running ${r.to} instead, `
                    + `${UNTIL[name] ?? 'until the backend restarts'}`
                    + (r.at ? ` (since ${new Date(r.at * 1000).toLocaleString()})` : ''),
                warn: true,
            });
        }
    }
    return lines;
}

/** A Strata row running the retained pin with the engine that pin names
 *  (`running_pin` another tag than the pin, `version_matches` not false): the
 *  version shown is the retained pin's own, not a mismatch with the pin. */
export function runsRetainedPin(row: {
    running_pin?: string | null; version_matches?: boolean | null; pin?: { release_tag: string } | null;
} | null | undefined): boolean {
    return !!row?.running_pin && !!row.pin && row.running_pin !== row.pin.release_tag
        && row.version_matches !== false;
}
