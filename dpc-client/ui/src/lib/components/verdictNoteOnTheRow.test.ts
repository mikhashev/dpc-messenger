/**
 * A message row says when a group record's signature was not, or could not be,
 * checked (DPC #111): the header carries verdictNote's text beside the time for
 * 'awaiting-certificate' and 'unsigned', and nothing for 'verified', 'legacy'
 * or an absent verdict — the absence of a verdict on old records must not be
 * drawn as an alarm, and a legacy record is age, not a threat.
 *
 * Vitest runs in `environment: 'node'` here and the repo carries no DOM
 * harness, so this reads the component source, as the neighbouring guards do.
 */

import { describe, it, expect } from "vitest";
import source from "./ChatMessageList.svelte?raw";

describe("the message header's verdict note", () => {
    it("renders verdictNote's words beside the time, as text", () => {
        expect(source).toMatch(/\{@const verdict = verdictNote\(msg\.verification\)\}/);
        expect(source).toMatch(/<span class="verdict-note">\{verdict\.text\}<\/span>/);
    });

    it("gates the note on exactly the two kinds that must be seen", () => {
        const gate = source.match(/\{#if (verdict\.kind === [^}]+)\} <span class="verdict-note">/);
        expect(gate?.[1]).toBe("verdict.kind === 'awaiting-certificate' || verdict.kind === 'unsigned'");
    });

    it("is styled quiet, not as an alarm", () => {
        const rule = source.match(/\.verdict-note\s*\{([^}]*)\}/)?.[1] ?? "";
        expect(rule).toContain("color: #999");
        expect(rule).not.toMatch(/#(e53e3e|fc8181|c53030|b3261e)/);
    });
});
