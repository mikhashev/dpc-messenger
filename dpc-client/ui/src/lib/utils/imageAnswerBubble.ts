/**
 * The chat bubble for an `ai_response_with_image` event.
 *
 * The event carries `sender_name` when the answer came from an agent (the name
 * its stored record carries), so the live bubble is headed like the one a
 * reload draws from history; an AI chat's event has none and the header falls
 * back to "AI (model)" as before.
 */

import type { AIResponseWithImageEvent } from '$lib/types';

export interface ImageAnswerBubble {
    id: string;
    sender: 'ai';
    senderName?: string;
    text: string;
    timestamp: number;
    model?: string;
}

export function imageAnswerBubble(
    event: AIResponseWithImageEvent,
    id: string,
    now: number,
): ImageAnswerBubble {
    const bubble: ImageAnswerBubble = {
        id,
        sender: 'ai',
        text: event.response,
        timestamp: now,
        model: event.model,
    };
    if (typeof event.sender_name === 'string' && event.sender_name) {
        bubble.senderName = event.sender_name;
    }
    return bubble;
}
