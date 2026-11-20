/**
 * A native <select> painted dark must drop native appearance.
 *
 * On Linux the Tauri webview is WebKitGTK, which draws its GTK menulist (light
 * under the default theme) over an author `background` unless the control has
 * `appearance: none`. The text colour is still the author's light one, so the
 * value sits light-on-near-white and cannot be read (Music Player's Library
 * list, Add Provider's Type / Context Window / Thinking). WebView2 on Windows
 * honours the author background, which is why it only showed up on Linux.
 *
 * The main panels are light and there is no global theme, so the fix is per
 * rule, not on :root: every select rule that paints a dark surface declares
 * `color-scheme: dark` and `appearance: none`, draws its own arrow with
 * `background-image`, and nothing later on the same selector resets that with
 * a `background:` or `padding:` shorthand (which would wipe the arrow or let
 * the text run under it).
 *
 * Vitest runs in `environment: 'node'` and the repo carries no DOM harness, so
 * this reads the component source rather than rendering it (the same bargain
 * approvalCardContrast.test.ts strikes). It guards the declarations, not the
 * painted pixels; those have to be looked at on a Linux node.
 */

import { describe, it, expect } from "vitest";

const sources = import.meta.glob(
    [
        "./ProvidersEditor.svelte",
        "./music/MusicPlayer.svelte",
        "./InstructionsEditor.svelte",
        "./AgentTaskBoard.svelte",
        "./InferenceSharingEditor.svelte",
        "../../routes/+page.svelte",
    ],
    { query: "?raw", import: "default", eager: true },
) as Record<string, string>;

/** Component → the selectors of its selects that sit on a dark surface. */
const DARK_SELECTS: Record<string, string[]> = {
    "./ProvidersEditor.svelte": [".form-group select"],
    "./music/MusicPlayer.svelte": [".list-select", ".add-to"],
    "./InstructionsEditor.svelte": [".edit-select", ".wizard-select"],
    "./AgentTaskBoard.svelte": [".agent-select", ".form-row select"],
    "./InferenceSharingEditor.svelte": ["select.inline-input"],
    "../../routes/+page.svelte": [".group-effort-select"],
};

const COLOR_SCHEME_DARK = /(^|[;\s])color-scheme\s*:\s*dark\b/;
const APPEARANCE_NONE = /(^|[;\s])appearance\s*:\s*none\b/;
const WEBKIT_APPEARANCE_NONE = /-webkit-appearance\s*:\s*none\b/;
const ARROW_IMAGE = /background-image\s*:\s*url\(/;
const BACKGROUND_SHORTHAND = /(^|[;\s])background\s*:/;
const PADDING_SHORTHAND = /(^|[;\s])padding\s*:/;

/** Selector → declarations, in source order, for the flat CSS these components use. */
function styleRules(source: string): Array<{ selectors: string[]; body: string }> {
    const style = source.match(/<style>([\s\S]*)<\/style>/);
    if (!style) return [];
    const withoutComments = style[1].replace(/\/\*[\s\S]*?\*\//g, "");
    const rules: Array<{ selectors: string[]; body: string }> = [];
    for (const match of withoutComments.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        const selectors = match[1].split(",").map((s) => s.trim().replace(/\s+/g, " "));
        rules.push({ selectors, body: match[2] });
    }
    return rules;
}

/** The rule targets this selector itself or one of its states (`:focus`, `:hover`). */
function targets(rule: { selectors: string[] }, selector: string): boolean {
    return rule.selectors.some((s) => s === selector || s.startsWith(`${selector}:`));
}

describe("a select painted dark drops native appearance (WebKitGTK)", () => {
    it("finds every component to check", () => {
        expect(Object.keys(sources).sort()).toEqual(Object.keys(DARK_SELECTS).sort());
    });

    for (const [path, selectors] of Object.entries(DARK_SELECTS)) {
        for (const selector of selectors) {
            it(`${path} ${selector} declares color-scheme: dark and appearance: none`, () => {
                const rules = styleRules(sources[path] ?? "").filter((rule) => targets(rule, selector));
                const fixIndex = rules.findIndex(
                    (rule) =>
                        rule.selectors.includes(selector) &&
                        COLOR_SCHEME_DARK.test(rule.body) &&
                        APPEARANCE_NONE.test(rule.body) &&
                        WEBKIT_APPEARANCE_NONE.test(rule.body),
                );
                expect(fixIndex).toBeGreaterThanOrEqual(0);

                const fix = rules[fixIndex];
                expect(fix.body).toMatch(ARROW_IMAGE);
                expect(fix.body).not.toMatch(BACKGROUND_SHORTHAND);
                // A padding shorthand in the same rule must come before padding-right.
                const shorthandAt = fix.body.search(PADDING_SHORTHAND);
                if (shorthandAt >= 0) {
                    expect(fix.body.indexOf("padding-right")).toBeGreaterThan(shorthandAt);
                }

                // Nothing later on the same selector wipes the arrow or the room for it.
                const later = rules.slice(fixIndex + 1);
                expect(later.filter((r) => BACKGROUND_SHORTHAND.test(r.body)).map((r) => r.selectors.join(", "))).toEqual([]);
                expect(later.filter((r) => PADDING_SHORTHAND.test(r.body)).map((r) => r.selectors.join(", "))).toEqual([]);
            });
        }

        it(`${path} has no multi-row select that a dropdown arrow would misdescribe`, () => {
            expect(sources[path] ?? "").not.toMatch(/<select\b[^>]*\b(multiple|size\s*=)/);
        });
    }
});
