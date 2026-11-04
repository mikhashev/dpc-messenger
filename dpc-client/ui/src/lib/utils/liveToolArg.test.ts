import { describe, it, expect } from 'vitest';
import { liveToolArgPreview } from './liveToolArg';
import { getToolArgPreview } from './toolDisplay';
import row from '../components/AgentProgressCollapsible.svelte?raw';
import list from '../components/ChatMessageList.svelte?raw';

describe('the live row shows what is running, as a finished call does', () => {
    it('a JSON argument string gives the same preview as the finished row', () => {
        const args = JSON.stringify({ command: 'npm run build', cwd: 'x' });
        expect(liveToolArgPreview('run_shell', args)).toBe('npm run build');
        expect(liveToolArgPreview('run_shell', args)).toBe(getToolArgPreview('run_shell', args));
        const read = JSON.stringify({ path: 'a/b.py' });
        expect(liveToolArgPreview('read_file', read)).toBe(getToolArgPreview('read_file', read));
    });

    it('the no-argument message and plain strings show nothing', () => {
        expect(liveToolArgPreview('run_shell', 'Executing run_shell...')).toBe('');
        expect(liveToolArgPreview('git_status', 'Executing git_status...')).toBe('');
        expect(liveToolArgPreview('run_shell', '✓ run_shell: done')).toBe('');
        expect(liveToolArgPreview('run_shell', 'null')).toBe('');
        expect(liveToolArgPreview('run_shell', '[1]')).toBe('');
        expect(liveToolArgPreview('run_shell', '{"command": "cut off')).toBe('');
    });

    it('a tool with no arguments, or none of the known ones, shows only its label', () => {
        expect(liveToolArgPreview('git_status', '{}')).toBe('');
        expect(liveToolArgPreview('git_status', '{"zzz": 1}')).toBe('');
        expect(liveToolArgPreview('run_shell', '{"command": 5}')).toBe('');
    });

    it('no tool means no preview, so a message never outlives its tool', () => {
        const args = JSON.stringify({ command: 'ls' });
        expect(liveToolArgPreview('', args)).toBe('');
        expect(liveToolArgPreview(null, args)).toBe('');
        expect(liveToolArgPreview('run_shell', null)).toBe('');
        // the next tool is judged by its own message only
        expect(liveToolArgPreview('read_file', JSON.stringify({ path: 'p.txt' }))).toBe('p.txt');
    });

    it('is wired: the list passes the message, the live row draws it in the finished style', () => {
        expect(list).toContain('currentMessage={agentProgressMessage}');
        const live = row.slice(row.indexOf('{#if isLive && currentTool}'));
        expect(live.slice(0, 600)).toContain('liveToolArgPreview(currentTool, currentMessage)');
        expect(live.slice(0, 600)).toContain('class="tool-arg"');
    });
});
