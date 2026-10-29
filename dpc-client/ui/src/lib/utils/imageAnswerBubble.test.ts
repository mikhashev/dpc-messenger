import { describe, it, expect } from 'vitest';
import { imageAnswerBubble } from './imageAnswerBubble';

describe('imageAnswerBubble', () => {
    it('is headed with the agent name when the event carries one', () => {
        const b = imageAnswerBubble(
            { conversation_id: 'agent_warren', response: 'a cat', provider: 'dpc_agent', model: 'agent', sender_name: 'Warren' },
            'id-1', 1000);
        expect(b.senderName).toBe('Warren');
        expect(b.sender).toBe('ai');
        expect(b.text).toBe('a cat');
    });

    it('leaves the header to the model name when the event has no sender_name', () => {
        const b = imageAnswerBubble(
            { conversation_id: 'local_ai', response: 'a dog', provider: 'x', model: 'qwen' },
            'id-2', 2000);
        expect(b.senderName).toBeUndefined();
        expect(b.model).toBe('qwen');
    });

    it('ignores an empty sender_name', () => {
        const b = imageAnswerBubble(
            { conversation_id: 'agent_w', response: 'r', sender_name: '' }, 'id-3', 3000);
        expect(b.senderName).toBeUndefined();
    });
});
