/**
 * «Чат 1:1 с агентом открыт, агент отвечает — по ходу ответ виден, а когда ответ
 * закончен, он исчезает. Чтобы увидеть его, надо переключиться на другой чат и
 * обратно» — Mike, 2026-08-07, and again on 2026-08-24: «а то раздражает уже».
 *
 * Both handlers that could wipe the live text are covered, because either one alone
 * reproduces the symptom and a fix to one of them looks complete while the other
 * still fires. The asymmetry to protect is the point: showing the answer twice for a
 * frame is a blemish, dropping it is a lost answer.
 */

import { describe, it, expect } from 'vitest';
import {
    historyUpdateApplies,
    mayDropLiveTextAfterHistoryUpdate,
    mayDropLiveTextOnCompletion,
} from './liveTextHandover';

describe('the backend payload replaces the list only when it is not shorter', () => {
    it('applies when it carries more, or the same', () => {
        expect(historyUpdateApplies(5, 4)).toBe(true);
        expect(historyUpdateApplies(4, 4)).toBe(true);
    });

    it('refuses a truncated payload, which is the guard that existed before', () => {
        expect(historyUpdateApplies(3, 4)).toBe(false);
        expect(historyUpdateApplies(0, 1)).toBe(false);
    });
});

describe('the history handler and the live text', () => {
    it('drops it only when the replacement actually landed', () => {
        expect(mayDropLiveTextAfterHistoryUpdate(true)).toBe(true);
        expect(mayDropLiveTextAfterHistoryUpdate(false)).toBe(false);
    });

    it('never drops it on the path where the update was refused', () => {
        // The whole defect in one line: the clear used to run before the update and
        // unconditionally, so a refusal left the text wiped and nothing put back.
        const applied = historyUpdateApplies(2, 4);
        expect(applied).toBe(false);
        expect(mayDropLiveTextAfterHistoryUpdate(applied)).toBe(false);
    });

    it('and the two decisions cannot drift apart', () => {
        for (const [incoming, existing] of [[0, 0], [1, 0], [3, 4], [4, 4], [9, 2]]) {
            const applied = historyUpdateApplies(incoming, existing);
            expect(mayDropLiveTextAfterHistoryUpdate(applied)).toBe(applied);
        }
    });
});

describe('task completion and the live text', () => {
    it('keeps it while the last message is still the user turn', () => {
        // The reply has not reached the history yet, so the live text is the only
        // copy — and this is the exact moment the user described it vanishing.
        expect(mayDropLiveTextOnCompletion({
            lastMessageSender: 'user', hasPendingCommand: false,
        })).toBe(false);
    });

    it('drops it once the history ends in a reply', () => {
        expect(mayDropLiveTextOnCompletion({
            lastMessageSender: 'agent_001', hasPendingCommand: false,
        })).toBe(true);
    });

    it('keeps it on an empty history, because absent is not a durable copy', () => {
        expect(mayDropLiveTextOnCompletion({
            lastMessageSender: null, hasPendingCommand: false,
        })).toBe(false);
        expect(mayDropLiveTextOnCompletion({
            lastMessageSender: undefined, hasPendingCommand: false,
        })).toBe(false);
    });

    it('keeps the pre-existing pending-command condition, and it wins', () => {
        expect(mayDropLiveTextOnCompletion({
            lastMessageSender: 'agent_001', hasPendingCommand: true,
        })).toBe(false);
    });
});
