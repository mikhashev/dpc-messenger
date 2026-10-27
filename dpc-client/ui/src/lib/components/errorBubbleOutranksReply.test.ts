/**
 * An error bubble in the chat must not lose to the ordinary reply rule.
 *
 * `.message:not(.user):not(.system)` paints every answer white and has the
 * specificity of three classes. `.message.error` has two, so for an assistant
 * or peer bubble the reply rule won on `background`, `border` and `margin-left`
 * and an error (also a turn that ended "failed") was drawn like an answer. The
 * muted note already carried a selector of the reply rule's specificity for the
 * same reason; the error rule needs one too, and, coming later in the file, it
 * then wins by order.
 *
 * Vitest runs in `environment: 'node'` here and the repo carries no DOM harness,
 * so this reads the component source and compares selector specificity. It
 * guards the cascade arithmetic, not the painted pixels.
 */

import { describe, it, expect } from "vitest";
import source from "./ChatMessageList.svelte?raw";

/** Selector → declarations, in file order, for the flat CSS this component uses. */
function styleRules(): Array<{ selector: string; body: string }> {
    const style = source.match(/<style>([\s\S]*)<\/style>/);
    if (!style) return [];
    const withoutComments = style[1].replace(/\/\*[\s\S]*?\*\//g, "");
    const rules: Array<{ selector: string; body: string }> = [];
    for (const match of withoutComments.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
        for (const selector of match[1].split(",")) {
            rules.push({ selector: selector.trim(), body: match[2] });
        }
    }
    return rules;
}

/** Class count of a compound selector, `:not(.x)` counting as its argument. */
function classCount(selector: string): number {
    return (selector.match(/\.[A-Za-z_-][\w-]*/g) ?? []).length;
}

describe("an error bubble outranks the reply rule", () => {
    const rules = styleRules();
    const reply = rules.findIndex((r) => r.selector === ".message:not(.user):not(.system)");
    const errors = rules
        .map((rule, index) => ({ ...rule, index }))
        .filter((r) => /^\.message\.error$|^\.message\.error:not\(/.test(r.selector) && /background/.test(r.body));

    it("finds the reply rule and an error rule that sets the background", () => {
        expect(reply).toBeGreaterThanOrEqual(0);
        expect(errors.length).toBeGreaterThan(0);
    });

    it("has an error selector at least as specific as the reply rule's, written after it", () => {
        const replyCount = classCount(rules[reply].selector);
        const winning = errors.filter((r) => classCount(r.selector) >= replyCount && r.index > reply);
        expect(winning.length).toBeGreaterThan(0);
    });
});
