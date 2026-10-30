import { describe, it, expect } from 'vitest';
import { mapBackendMessage } from './messageMapper';

/**
 * A check_back or chat task that answers in a 1:1 agent chat writes its prompt
 * into that chat's history under this node's id, named "Scheduled <task_type>"
 * and marked with `task_type`. The identity path took this node's id for the
 * owner and drew the prompt as "You", while the paged reload, which passes no
 * identity, showed "Scheduled check_back": one record, two authors. See
 * A-TASKS-ANSWER-IN-A-ONE-TO-ONE-AGENT-CHAT-IS-NEVER-SHOWN on the board.
 */

const NODE = 'dpc-node-' + '1'.repeat(32);
const identity = { agentSelfId: 'agent_001', agentSelfName: 'Ark', selfNodeId: NODE };

const scheduled = {
    id: 'a1b2c3d4e5f60718',
    role: 'user',
    content: 'Check whether the restore finished.',
    timestamp: '2026-10-30T09:00:00+00:00',
    sender_node_id: NODE,
    sender_name: 'Scheduled check_back',
    task_type: 'check_back',
    msg_index: 7,
};

describe('a scheduled task prompt in an agent chat', () => {
    it('is not drawn as the owner\'s own message', () => {
        const mapped = mapBackendMessage(scheduled, { identity });
        expect(mapped.sender).not.toBe('user');
        expect(mapped.senderName).toBe('Scheduled check_back');
        expect(mapped.isAgent).toBe(false);
    });

    it('has the same author down the paged reload, which passes no identity', () => {
        const withIdentity = mapBackendMessage(scheduled, { identity });
        const paged = mapBackendMessage(scheduled, {});
        expect(paged.sender).toBe(withIdentity.sender);
        expect(paged.senderName).toBe(withIdentity.senderName);
    });

    it('leaves the owner\'s own question drawn as theirs', () => {
        const { task_type: _, ...own } = { ...scheduled, sender_name: 'Mike' };
        expect(mapBackendMessage(own, { identity }).sender).toBe('user');
    });
});
