import { describe, expect, it } from 'vitest';
import { transferRowLabel } from './transferRowLabel';

const HOP = 'dpc-node-' + 'b'.repeat(32);
const AUTHOR = 'dpc-node-' + 'c'.repeat(32);
const GROUP = 'group-1234abcd';
const short = (id: string) => `${id.slice(0, 20)}…`;

const names: Record<string, string> = { [HOP]: 'Bob', [AUTHOR]: 'Cara' };
const nameOf = (id: string) => names[id];
const groupNameOf = (id: string) => (id === GROUP ? 'Team' : undefined);

describe('an incoming transfer', () => {
    it('names the group and the sender of a group file', () => {
        const row = { node_id: HOP, sender_name: 'Bob', group_id: GROUP, sender_node_id: HOP };
        expect(transferRowLabel(row, nameOf, groupNameOf)).toBe(`in Team · from Bob (${short(HOP)})`);
    });

    it('names the author and the node it came through when they differ', () => {
        const row = { node_id: HOP, sender_name: 'Bob', group_id: GROUP, sender_node_id: AUTHOR };
        expect(transferRowLabel(row, nameOf, groupNameOf))
            .toBe(`in Team · from Cara (${short(AUTHOR)}) via Bob (${short(HOP)})`);
    });

    it('says only the sender of a one-to-one file', () => {
        expect(transferRowLabel({ node_id: HOP, sender_name: 'Bob' }, nameOf, groupNameOf)).toBe(`from Bob (${short(HOP)})`);
    });

    it('takes the name it knows when the offer named the sender by its id', () => {
        expect(transferRowLabel({ node_id: HOP, sender_name: HOP }, nameOf)).toBe(`from Bob (${short(HOP)})`);
        expect(transferRowLabel({ node_id: HOP, sender_name: HOP })).toBe(`from ${short(HOP)}`);
    });

    it('falls back to the group id when the group is not known here', () => {
        const row = { node_id: HOP, sender_name: 'Bob', group_id: 'group-unknown' };
        expect(transferRowLabel(row, nameOf, groupNameOf)).toBe(`in group-unknown · from Bob (${short(HOP)})`);
    });
});

describe('an outgoing transfer', () => {
    it('names the peer', () => {
        expect(transferRowLabel({ node_id: HOP, direction: 'upload' }, nameOf, groupNameOf)).toBe(`to Bob (${short(HOP)})`);
    });

    it('names the group it is sent in', () => {
        const row = { node_id: HOP, direction: 'upload', group_id: GROUP };
        expect(transferRowLabel(row, nameOf, groupNameOf)).toBe(`in Team · to Bob (${short(HOP)})`);
    });
});
