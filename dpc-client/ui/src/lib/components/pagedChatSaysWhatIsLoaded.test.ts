/**
 * A paged chat says what is loaded rather than implying it is everything.
 *
 * The chat window shows one line at the top once the pager has nothing older
 * (startLineText in historyPaging.ts, tested there), and SessionControls'
 * count is of the messages loaded on screen, not of the chat's live history.
 *
 * Vitest runs in `environment: 'node'` here and the repo carries no DOM
 * harness, so this reads the component sources.
 */

import { describe, it, expect } from "vitest";
import chatList from "./ChatMessageList.svelte?raw";
import sessionControls from "./SessionControls.svelte?raw";

describe("the chat window's top line", () => {
    it("comes from the pager through startLineText, the one line for both ends", () => {
        expect(chatList).toMatch(/startLineText\(pager, displayMessages\.length > 0\)/);
        expect(chatList).toMatch(/\{#if startLine\}\s*<div class="history-boundary">\{startLine\}<\/div>/);
        // A second, separate boundary line would draw two lines at the top.
        expect(chatList).not.toMatch(/boundaryLineText\(/);
    });
});

describe("SessionControls' message count", () => {
    it("is labelled as loaded", () => {
        const row = sessionControls.match(/<div class="token-row[^"]*"[^>]*>\s*<span class="token-label">([^<]*)<\/span>\s*<span class="token-value">\{messageCount\}<\/span>/);
        expect(row?.[1]).toBe("Loaded");
        expect(sessionControls).not.toMatch(/title="Number of messages in current conversation"/);
    });
});
