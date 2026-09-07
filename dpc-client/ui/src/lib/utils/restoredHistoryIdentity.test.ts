import { describe, it, expect } from 'vitest';
import { mapBackendMessage } from './messageMapper';
import { mergeBackfillWithLive } from './liveMessageIdentity';

/**
 * One stored record reaches the screen down two paths that spell its id
 * differently: `export_history` writes `id` and no `message_id`, while
 * `get_conversation_history` fills `message_id` from `id`. Reading only one
 * spelling makes the two copies unmatchable and the conversation is drawn
 * twice. See A-RESTORED-HISTORY-IS-DRAWN-BESIDE-ITS-OWN-BACKFILL… on the board.
 */

const STORED_ID = 'f3a91c0e5d7b4a12';

/** Shaped like conversation_monitor.export_history. */
const exported = {
    id: STORED_ID,
    role: 'user',
    content: 'Привет, как дела?',
    timestamp: '2026-09-07T09:51:37+00:00',
    sender_node_id: 'dpc-node-' + '0'.repeat(32),
    sender_name: 'New User',
    sender_type: 'human',
};

/** Shaped like service.get_conversation_history. */
const fromBackend = { ...exported, msg_index: 1, chain_hash: 'deadbeef', message_id: STORED_ID };

const restore = () => mapBackendMessage(exported, { index: 0, totalCount: 2 });
const backfill = () => mapBackendMessage(fromBackend, { index: 0, totalCount: 2 });

describe('a restored history and its own backfill', () => {
    it('gives the same record one id down both paths', () => {
        expect(restore().id).toBe(STORED_ID);
        expect(backfill().id).toBe(STORED_ID);
    });

    it('draws the message once when the backfill lands on the restore', () => {
        expect(mergeBackfillWithLive([backfill()], [restore()])).toHaveLength(1);
    });

    it('draws it twice if the restore mints an id of its own', () => {
        const synthetic = { ...backfill(), id: `restored-0-${Date.now()}` };
        expect(mergeBackfillWithLive([backfill()], [synthetic])).toHaveLength(2);
    });

    it('carries the sender identity a hand-built row dropped', () => {
        const restored = mapBackendMessage(exported, {
            fallbackSender: exported.sender_node_id,
            fallbackSenderName: exported.sender_name,
            index: 0,
            totalCount: 2,
        });

        expect(restored.sender).toBe(exported.sender_node_id);
        expect(restored.senderName).toBe('New User');
    });

    it('takes msg_index from the backfill, which is the only path that carries it', () => {
        expect(restore().msg_index).toBe(0);
        expect(backfill().msg_index).toBe(1);
    });

    it('keeps a live message that landed while the restore was in flight', () => {
        const live = { id: 'arrived-meanwhile', text: 'typed just now', timestamp: Date.now() };

        const merged = mergeBackfillWithLive([restore()], [live]);
        expect(merged).toHaveLength(2);
        expect(merged[1].id).toBe('arrived-meanwhile');
    });
});
