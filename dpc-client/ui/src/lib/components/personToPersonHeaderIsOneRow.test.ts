/**
 * A chat with another person's node has the Text/Markdown switch, and its
 * header controls sit in one row.
 *
 * Card A-PERSON-TO-PERSON-CHAT-HAS-NO-TEXT-MARKDOWN-TOGGLE-THAT-AI-AND-GROUP-CHATS-HAVE:
 * SessionControls drew the toggle only for `isAIChat || group-…`, and a P2P chat
 * id is the peer's node id (`dpc-node-…`), so the switch never showed there even
 * though the flag is one global value (+page.svelte, localStorage
 * `enableMarkdown`) that ChatMessageList already renders by in every chat.
 *
 * The same header had Block beside the title and Auto Transcribe in the title
 * column, each pushed right by its own trick (a 100%-wide title button, a
 * `margin-left: auto`), so they landed as a small badge stacked above a pill,
 * off the line of New Session / Extract Knowledge. Both now render first inside
 * SessionControls' own `.chat-actions` row through the `leadingActions`
 * snippet, with the buttons' metrics.
 *
 * Vitest runs in `environment: 'node'` with no DOM harness, so this reads the
 * component sources (the bargain approvalCardContrast.test.ts strikes). It
 * guards the condition and the layout rules, not painted pixels.
 */

import { describe, it, expect } from "vitest";
import controls from "./SessionControls.svelte?raw";
import messageList from "./ChatMessageList.svelte?raw";
import page from "../../routes/+page.svelte?raw";

/** Selector → declarations, for the flat CSS these components use. */
function styleRule(source: string, selector: string): string {
    const style = source.match(/<style>([\s\S]*)<\/style>/);
    if (!style) return "";
    const css = style[1].replace(/\/\*[\s\S]*?\*\//g, "");
    for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        const selectors = match[1].split(",").map((s) => s.trim());
        if (selectors.includes(selector)) return match[2];
    }
    return "";
}

/** The markup only: script and style blocks removed. */
function markup(source: string): string {
    return source
        .replace(/<script[\s\S]*?<\/script>/g, "")
        .replace(/<style>[\s\S]*<\/style>/g, "");
}

/** The body of the page's `leadingActions` snippet. */
function leadingSnippet(): string {
    const m = page.match(/\{#snippet leadingActions\(\)\}([\s\S]*?)\{\/snippet\}/);
    return m ? m[1] : "";
}

describe("the Text/Markdown switch shows in a person-to-person chat", () => {
    it("SessionControls gates the toggle on AI, group or P2P", () => {
        const derived = controls.match(/let showMarkdownToggle = \$derived\(([\s\S]*?)\);/);
        expect(derived).not.toBeNull();
        const condition = derived![1];
        expect(condition).toContain("isAIChat");
        expect(condition).toMatch(/startsWith\(['"]group-['"]\)/);
        expect(condition).toContain("isP2PChat");
    });

    it("the toggle button sits under that derived condition and nothing narrower", () => {
        const m = markup(controls).match(/\{#if ([^}]*)\}\s*<button\s+class="btn-markdown-toggle"/);
        expect(m).not.toBeNull();
        expect(m![1].trim()).toBe("showMarkdownToggle");
    });

    it("the page tells SessionControls a chat is P2P", () => {
        const tag = page.match(/<SessionControls\b([\s\S]*?)>/);
        expect(tag).not.toBeNull();
        expect(tag![1]).toMatch(/\{isP2PChat\}|isP2PChat=\{isP2PChat\}/);
    });

    it("one flag feeds the switch and the message list, and the list renders by it in every chat", () => {
        expect(page).toMatch(/localStorage\.getItem\(['"]enableMarkdown['"]\)/);
        const sessionTag = page.match(/<SessionControls\b[\s\S]*?>/)![0];
        const listTag = page.match(/<ChatMessageList\b[\s\S]*?\/>/)![0];
        expect(sessionTag).toContain("bind:enableMarkdown");
        expect(listTag).toContain("bind:enableMarkdown");
        // No chat-kind test wrapped around the markdown branch.
        expect(messageList).toMatch(/\{#if enableMarkdown\}\s*<MarkdownMessage content=\{msg\.text\} \/>/);
    });
});

describe("a person-to-person header is one row of controls", () => {
    it("SessionControls renders the page's controls first in its .chat-actions row", () => {
        const row = markup(controls).match(/<div class="chat-actions">([\s\S]*?)<button class="btn-new-chat"/);
        expect(row).not.toBeNull();
        expect(row![1].trim()).toBe("{@render leadingActions?.()}");
    });

    it("Block and Auto Transcribe live only in that snippet, not in the title column", () => {
        const snippet = leadingSnippet();
        expect(snippet).toContain('class="peer-block-btn"');
        expect(snippet).toContain('class="auto-transcribe-toggle"');
        expect(snippet).toMatch(/\{#if voiceMemoryNote\(/);

        const outside = markup(page).replace(/\{#snippet leadingActions\(\)\}[\s\S]*?\{\/snippet\}/, "");
        expect(outside).not.toContain("peer-block-btn");
        expect(outside).not.toContain("auto-transcribe-toggle");
        expect(outside).not.toContain("voice-memory-note");
    });

    it("the row is a wrapping, vertically centred flex line", () => {
        const row = styleRule(controls, ".chat-actions");
        expect(row).toMatch(/display:\s*flex/);
        expect(row).toMatch(/flex-wrap:\s*wrap/);
        expect(row).toMatch(/align-items:\s*center/);
    });

    for (const selector of [".peer-block-btn", ".auto-transcribe-toggle"]) {
        it(`${selector} takes the buttons' metrics and the row's height`, () => {
            const rule = styleRule(page, selector);
            expect(rule).toMatch(/align-self:\s*stretch/);
            expect(rule).toMatch(/font-size:\s*0\.9rem/);
            expect(rule).toMatch(/border-radius:\s*6px/);
            expect(rule).toMatch(/box-sizing:\s*border-box/);
            // The old per-item push to the right is what made the stack ragged.
            expect(rule).not.toMatch(/margin-left:\s*auto/);
        });
    }

    it("Block matches the buttons' height; the label stays shorter so the buttons set the row", () => {
        // A button beside .btn-new-chat (padding 0.6rem 1rem, no border): same padding less its border.
        expect(styleRule(controls, ".btn-new-chat")).toMatch(/padding:\s*0\.6rem 1rem/);
        expect(styleRule(page, ".peer-block-btn")).toMatch(/padding:\s*calc\(0\.6rem - 1px\) 1rem/);
        // The label is in the body font, whose line-height runs taller than a
        // button's in Chromium; with less vertical padding it never sets the
        // line, and align-self: stretch lifts it to the buttons' height.
        const label = styleRule(page, ".auto-transcribe-toggle").match(/padding:\s*([\d.]+)rem 1rem/);
        expect(label).not.toBeNull();
        expect(Number(label![1])).toBeLessThan(0.6);
    });

    it("the voice-memory note wraps to a line of its own under the row", () => {
        const rule = styleRule(page, ".voice-memory-note");
        expect(rule).toMatch(/flex:\s*0 0 100%/);
        expect(rule).toMatch(/order:\s*1/);
    });
});
