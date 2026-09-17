/**
 * An approval card that paints itself dark must also say what colour its text is.
 *
 * Nothing in this app declares a text colour on `body` or on `:root` — there is
 * no theme stylesheet at all, and every `var(--x, …)` in the components resolves
 * to its own fallback. So text with no `color` of its own inherits the browser
 * default, which is black: right on the light panels, invisible on a card whose
 * background is `--bg-secondary` (#1e1e2e). That is how the line naming the
 * matched rule — `Requires approval: \bdocker\b` — came to sit at 1.21:1 against
 * its own card while every neighbour that had been given a colour read normally.
 *
 * The guard is on the pairing, not on a hex: a rule that sets the dark surface
 * has to set the token for text on it in the same rule. The glob takes in every
 * approval dialog in this directory, so a card added later is covered without
 * anyone remembering to list it.
 *
 * Vitest runs in `environment: 'node'` here and the repo carries no DOM harness,
 * so this reads the component source rather than rendering it. That buys the
 * pairing rule and nothing about the painted pixels; the contrast figures
 * themselves were computed separately and recorded in the commit message.
 */

import { describe, it, expect } from "vitest";

/** Every floating approval card component beside this test. */
const approvalDialogs = import.meta.glob("./*ApprovalDialog.svelte", {
    query: "?raw",
    import: "default",
    eager: true,
}) as Record<string, string>;

const DARK_SURFACE = /background\s*:\s*var\(\s*--bg-secondary\b/;
const TEXT_ON_DARK = /color\s*:\s*var\(\s*--text-primary\b/;

/** Selector → declarations, for the flat CSS these components use. */
function styleRules(source: string): Array<{ selector: string; body: string }> {
    const style = source.match(/<style>([\s\S]*)<\/style>/);
    if (!style) return [];
    const withoutComments = style[1].replace(/\/\*[\s\S]*?\*\//g, "");
    const rules: Array<{ selector: string; body: string }> = [];
    for (const match of withoutComments.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        rules.push({ selector: match[1].trim(), body: match[2] });
    }
    return rules;
}

describe("an approval card that paints a dark surface names its text colour", () => {
    it("finds the approval dialogs to check", () => {
        expect(Object.keys(approvalDialogs).length).toBeGreaterThanOrEqual(2);
    });

    for (const [path, source] of Object.entries(approvalDialogs)) {
        it(`${path} pairs --bg-secondary with --text-primary`, () => {
            const darkRules = styleRules(source).filter((rule) => DARK_SURFACE.test(rule.body));

            // A component that stopped using the dark surface would pass vacuously.
            expect(darkRules.length).toBeGreaterThan(0);

            const unpaired = darkRules
                .filter((rule) => !TEXT_ON_DARK.test(rule.body))
                .map((rule) => rule.selector);
            expect(unpaired).toEqual([]);
        });
    }
});
