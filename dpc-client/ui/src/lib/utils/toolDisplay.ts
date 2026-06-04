/**
 * Human-readable tool display labels and categories.
 * Maps raw tool names from registry to user-facing labels.
 * English only — i18n deferred until full UI localization.
 */

export type ToolCategory = 'search' | 'files' | 'git' | 'browser' | 'comfyui' | 'memory' | 'tasks' | 'skills' | 'shell' | 'messaging' | 'archive' | 'auth';

interface ToolDisplayEntry {
    label: string;
    category: ToolCategory;
    argExtract?: (input: string) => string;
}

function extractFirst(input: string, key: string): string {
    if (!input) return '';
    try {
        const obj = typeof input === 'string' ? JSON.parse(input) : input;
        return obj[key] || '';
    } catch {
        const short = input.length > 80 ? input.slice(0, 80) + '...' : input;
        return short;
    }
}

const pathArg = (input: string) => extractFirst(input, 'path') || extractFirst(input, 'file_path');
const patternArg = (input: string) => extractFirst(input, 'pattern') || extractFirst(input, 'query');
const urlArg = (input: string) => extractFirst(input, 'url');
const cmdArg = (input: string) => extractFirst(input, 'command');
// Fallback for tools without a specific argExtract — surface the most relevant arg so
// EVERY tool shows WHAT it operated on (which file / pattern / url / command / ...).
const genericArg = (input: string) =>
    extractFirst(input, 'path') || extractFirst(input, 'file_path') ||
    extractFirst(input, 'pattern') || extractFirst(input, 'query') ||
    extractFirst(input, 'url') || extractFirst(input, 'command') ||
    extractFirst(input, 'name') || extractFirst(input, 'ref') ||
    extractFirst(input, 'branch') || extractFirst(input, 'message');
const nameArg = (input: string) => extractFirst(input, 'name');
const msgArg = (input: string) => {
    const t = extractFirst(input, 'text') || extractFirst(input, 'message');
    return t.length > 60 ? t.slice(0, 60) + '...' : t;
};

const TOOL_DISPLAY: Record<string, ToolDisplayEntry> = {
    search_files:    { label: 'Search files',       category: 'search', argExtract: patternArg },
    search_in_file:  { label: 'Search in file',     category: 'search', argExtract: patternArg },
    search_web:      { label: 'Web search',         category: 'search', argExtract: patternArg },
    memory_search:   { label: 'Memory search',      category: 'search', argExtract: patternArg },

    read_file:       { label: 'Read file',          category: 'files', argExtract: pathArg },
    write_file:      { label: 'Write file',         category: 'files', argExtract: pathArg },
    list_dir:        { label: 'List directory',      category: 'files', argExtract: pathArg },
    repo_delete:     { label: 'Delete file',        category: 'files', argExtract: pathArg },
    list_extended_sandbox_paths: { label: 'Sandbox paths', category: 'files' },

    update_scratchpad:     { label: 'Update scratchpad',     category: 'memory' },
    update_identity:       { label: 'Update identity',       category: 'memory' },
    deduplicate_identity:  { label: 'Dedup identity',        category: 'memory' },
    chat_history:          { label: 'Chat history',          category: 'memory' },
    knowledge_list:        { label: 'Knowledge list',        category: 'memory' },
    get_dpc_context:       { label: 'DPC context',           category: 'memory' },

    schedule_task:       { label: 'Schedule task',       category: 'tasks', argExtract: nameArg },
    get_task_board:      { label: 'Task board',          category: 'tasks' },
    get_task_status:     { label: 'Task status',         category: 'tasks' },
    register_task_type:  { label: 'Register task type',  category: 'tasks' },
    list_task_types:     { label: 'Task types',          category: 'tasks' },
    unregister_task_type:{ label: 'Remove task type',    category: 'tasks' },

    git_status:   { label: 'Git status',    category: 'git' },
    git_diff:     { label: 'Git diff',      category: 'git' },
    git_log:      { label: 'Git log',       category: 'git' },
    git_branch:   { label: 'Git branch',    category: 'git' },
    git_add:      { label: 'Git add',       category: 'git', argExtract: pathArg },
    git_commit:   { label: 'Git commit',    category: 'git', argExtract: msgArg },
    git_init:     { label: 'Git init',      category: 'git' },
    git_checkout: { label: 'Git checkout',  category: 'git' },
    git_merge:    { label: 'Git merge',     category: 'git' },
    git_tag:      { label: 'Git tag',       category: 'git' },
    git_reset:    { label: 'Git reset',     category: 'git' },
    git_snapshot: { label: 'Git snapshot',  category: 'git' },
    git_push:     { label: 'Git push',      category: 'git' },

    browse_page:       { label: 'Browse page',     category: 'browser', argExtract: urlArg },
    fetch_json:        { label: 'Fetch JSON',      category: 'browser', argExtract: urlArg },
    check_url:         { label: 'Check URL',       category: 'browser', argExtract: urlArg },
    browser_snapshot:  { label: 'Page snapshot',   category: 'browser' },
    browser_navigate:  { label: 'Navigate',        category: 'browser', argExtract: urlArg },
    browser_scroll:    { label: 'Scroll',          category: 'browser' },
    browser_click:     { label: 'Click',           category: 'browser' },
    browser_fill:      { label: 'Fill field',      category: 'browser' },
    browser_wait_for:  { label: 'Wait for',        category: 'browser' },
    browser_extract:   { label: 'Extract data',    category: 'browser' },
    browser_screenshot:{ label: 'Screenshot',      category: 'browser' },
    browser_switch_tab:{ label: 'Switch tab',      category: 'browser' },
    browser_collect:   { label: 'Collect data',    category: 'browser' },
    browser_close:     { label: 'Close browser',   category: 'browser' },

    comfyui_submit:       { label: 'ComfyUI submit',   category: 'comfyui' },
    comfyui_check:        { label: 'ComfyUI check',    category: 'comfyui' },
    comfyui_wait:         { label: 'ComfyUI wait',     category: 'comfyui' },
    comfyui_queue_status: { label: 'ComfyUI queue',    category: 'comfyui' },
    comfyui_progress:     { label: 'ComfyUI progress', category: 'comfyui' },
    comfyui_convert:      { label: 'ComfyUI convert',  category: 'comfyui' },

    run_shell: { label: 'Shell command', category: 'shell', argExtract: cmdArg },

    send_user_message: { label: 'Send message', category: 'messaging', argExtract: msgArg },

    read_session_archive:    { label: 'Read archive',       category: 'archive' },
    read_session_detail:     { label: 'Session detail',     category: 'archive' },
    search_session_archives: { label: 'Search archives',    category: 'archive', argExtract: patternArg },

    list_my_tools:          { label: 'My tools',          category: 'skills' },
    list_my_skills:         { label: 'My skills',         category: 'skills' },
    list_local_agents:      { label: 'Local agents',      category: 'skills' },
    list_agent_skills:      { label: 'Agent skills',      category: 'skills' },
    import_skill_from_agent:{ label: 'Import skill',      category: 'skills' },
    execute_skill:          { label: 'Execute skill',     category: 'skills', argExtract: nameArg },

    list_auth_domains: { label: 'Auth domains', category: 'auth' },
};

const CATEGORY_LABELS: Record<ToolCategory | 'other', string> = {
    search: 'SEARCH', files: 'FILES', git: 'GIT', browser: 'BROWSER',
    comfyui: 'COMFYUI', memory: 'MEMORY', tasks: 'TASKS', skills: 'SKILLS',
    shell: 'SHELL', messaging: 'MESSAGES', archive: 'ARCHIVE', auth: 'AUTH', other: 'OTHER',
};

export function getToolLabel(toolName: string): string {
    return TOOL_DISPLAY[toolName]?.label || toolName.replace(/_/g, ' ');
}

export function getToolCategory(toolName: string): ToolCategory | 'other' {
    return TOOL_DISPLAY[toolName]?.category || 'other';
}

export function getCategoryLabel(category: ToolCategory | 'other'): string {
    return CATEGORY_LABELS[category] || category.toUpperCase();
}

export function getActionLabel(count: number): string {
    return `Actions — ${count} ${count === 1 ? 'action' : 'actions'}`;
}

export function getToolArgPreview(toolName: string, input: string): string {
    const entry = TOOL_DISPLAY[toolName];
    // Specific extractor when defined, otherwise a generic fallback so tools like
    // git_diff / git_log (no extractor) still show their target instead of nothing.
    if (entry?.argExtract) return entry.argExtract(input) || '';
    return genericArg(input);
}
