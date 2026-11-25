import { describe, it, expect } from 'vitest';
import { approvalView, askerLine } from './scheduleApprovalText';
import { ownNodeGroupOptions, withOwnNodeGroup, ownNodeGroupLabel } from './ownNodeGroup';
import shellDialog from '../components/ShellApprovalDialog.svelte?raw';
import panel from '../components/AgentPermissionsPanel.svelte?raw';
import editor from '../components/FirewallEditor.svelte?raw';

// What the backend sends for a flagged tool a friend asked for (ADR-053,
// owner_approval.await_owner_approval via tools.core._await_schedule_approval).
const friendCard = {
  task_type: 'owner_approval',
  when: 'now',
  about: 'Miron asks to run write_file: write 5 characters to C:/agent/notes.txt',
  asker: { name: 'Miron', node_id: 'dpc-node-friend-0001', kind: 'human', verified: true, via: [], label: 'Miron' },
  tool: 'write_file',
  details: 'write 5 characters to C:/agent/notes.txt',
  cost: null,
};

describe('an owner card for a foreign request names who asked, the tool and what exactly', () => {
  const v = approvalView(friendCard);
  it('heads with the tool and says the asker is not the owner', () => {
    expect(v.headText).toBe('asks to run write_file for someone who is not the owner');
    expect(v.approveLabel).toBe('Approve');
    expect(v.refuseLabel).toBe('Refuse');
  });
  it('shows asked by, tool and what exactly', () => {
    expect(v.rows.map((r) => r.label)).toEqual(['Asked by', 'Tool', 'What exactly']);
    expect(v.rows[0].value).toBe('Miron · human · node dpc-node-friend-0001');
    expect(v.rows[2].value).toContain('notes.txt');
  });
  it('states the cost when the tool spends the GPU card', () => {
    const gpu = approvalView({ ...friendCard, tool: 'describe_image', cost: 'Uses the GPU card: about 25 s' });
    expect(gpu.rows.at(-1)).toEqual({ label: 'Cost', value: 'Uses the GPU card: about 25 s', long: true });
  });
});

describe('the asker line', () => {
  it('says agent, human, or not verified, and the relay', () => {
    expect(askerLine({ name: 'Bot', kind: 'agent', node_id: 'n1' })).toBe('Bot · agent · node n1');
    expect(askerLine({ name: 'X', kind: 'unknown', node_id: 'n2', label: 'X (not verified)' }))
      .toBe('X (not verified) · not verified · node n2');
    expect(askerLine({ label: 'Miron via Ark', kind: 'human' })).toBe('Miron via Ark · human');
    expect(askerLine(null)).toBe('');
  });
});

describe('the existing cards carry the asker when one is sent (D-b), and stay as they were otherwise', () => {
  it('a check_back from a friend adds the Asked by row first', () => {
    const v = approvalView({ task_type: 'check_back', when: 'in 60s', about: 'look', asker: friendCard.asker });
    expect(v.rows.map((r) => r.label)).toEqual(['Asked by', 'When', 'What it will do']);
  });
  it('a music job from a friend names the asker and the cost', () => {
    const v = approvalView({ task_type: 'music_job', when: 'now', about: 'x', asker: friendCard.asker, cost: 'Uses the GPU card' });
    expect(v.rows[0].label).toBe('Asked by');
    expect(v.rows.at(-1)?.label).toBe('Cost');
  });
  it('the owner own check_back card is unchanged', () => {
    const v = approvalView({ task_type: 'check_back', when: 'in 60s', about: 'look' });
    expect(v.rows.map((r) => r.label)).toEqual(['When', 'What it will do']);
  });
  it('the shell card shows the asker and the cwd, and offers no whitelist for a foreign request', () => {
    expect(shellDialog).toContain('Asked by {askerLine(request.asker)}');
    expect(shellDialog).toContain('{#if request.cwd}');
    expect(shellDialog).toContain('{#if !request.asker}');
  });
});

describe('the own_node_group picker (ADR-053 step 7)', () => {
  it('offers the node_groups names, without comment keys', () => {
    expect(ownNodeGroupOptions({ laptops: [], _comment: 'x', desk: [] })).toEqual(['desk', 'laptops']);
    expect(ownNodeGroupOptions(null)).toEqual([]);
  });
  it('writes memory.own_node_group, and choosing none leaves the field absent', () => {
    expect(withOwnNodeGroup({ enabled: true }, 'laptops')).toEqual({ enabled: true, own_node_group: 'laptops' });
    const cleared = withOwnNodeGroup({ enabled: true, own_node_group: 'laptops' }, '');
    expect('own_node_group' in cleared).toBe(false);
    expect(ownNodeGroupLabel({})).toBe('none (only this node is the owner)');
  });
  it('is in the panel and fed from the firewall editor', () => {
    expect(panel).toContain('id="agent-own-node-group"');
    expect(panel).toContain('ownNodeGroupOptions(nodeGroups)');
    expect(editor).toContain('nodeGroups={(editMode ? editedRules : rules)?.node_groups ?? null}');
  });
});
