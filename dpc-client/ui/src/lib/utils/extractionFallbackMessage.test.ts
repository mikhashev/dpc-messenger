import { describe, it, expect } from 'vitest';
import { formatExtractionFallbackMessage } from './extractionFallbackMessage';

describe('formatExtractionFallbackMessage', () => {
    it('names the peer, the declined alias, the reason and the alias that answered', () => {
        const message = formatExtractionFallbackMessage({
            conversation_id: 'conv-1',
            node_id: 'dpc-node-0123456789abcdef0123456789abcdef',
            requested_alias: 'remote:dpc-node-0123456789abcdef0123456789abcdef:big',
            reason: 'compute sharing disabled for this peer',
            fallback_alias: 'local-cold',
        });

        expect(message).toContain('dpc-node-0123456789abcdef0123456789abcdef'.slice(0, 20));
        expect(message).toContain('remote:dpc-node-0123456789abcdef0123456789abcdef:big');
        expect(message).toContain('compute sharing disabled for this peer');
        expect(message).toContain('local-cold');
    });

    it('falls back to generic wording when the peer or alias is unknown', () => {
        const message = formatExtractionFallbackMessage({
            conversation_id: null,
            node_id: null,
            requested_alias: null,
            reason: 'timeout',
            fallback_alias: 'local-cold',
        });

        expect(message).toContain('A peer');
        expect(message).toContain('the requested provider');
        expect(message).toContain('timeout');
        expect(message).toContain('local-cold');
    });
});
