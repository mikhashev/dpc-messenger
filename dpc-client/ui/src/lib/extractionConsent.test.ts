import { describe, it, expect } from 'vitest';
import editor from './components/FirewallEditor.svelte?raw';
import {
  CONSENT_TARGET_FIELDS, cancelOutcome, consentedPayload, extractionConsentQuestion, isConsentRequired, rememberLabel, rememberedEntryLine, requestExtraction,
  withCurrentRemembered,
} from './extractionConsent';

const PEER = 'dpc-node-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';

describe('isConsentRequired', () => {
  it('reads only the consent answer', () => {
    expect(isConsentRequired({ status: 'consent_required', provider_alias: 'x' })).toBe(true);
    expect(isConsentRequired({ status: 'success' })).toBe(false);
    expect(isConsentRequired({ status: 'error' })).toBe(false);
    expect(isConsentRequired(false)).toBe(false);
    expect(isConsentRequired(undefined)).toBe(false);
    expect(isConsentRequired(null)).toBe(false);
  });
});

describe('extractionConsentQuestion', () => {
  it('names a vendor model as cloud', () => {
    expect(extractionConsentQuestion({
      status: 'consent_required', provider_alias: 'deepseek_flash', provider_type: 'deepseek',
      provider_kind: 'vendor', compute_host: null,
    })).toBe('Extract Knowledge will send this conversation to deepseek_flash (deepseek, cloud). Continue?');
  });

  it('says unknown for a kind the node cannot tell', () => {
    expect(extractionConsentQuestion({
      status: 'consent_required', provider_alias: 'gone_alias', provider_type: null,
      provider_kind: 'unknown', compute_host: null,
    })).toBe('Extract Knowledge will send this conversation to gone_alias (unknown). Continue?');
  });

  it('says a machine on your network for a local engine on a LAN host', () => {
    expect(extractionConsentQuestion({
      status: 'consent_required', provider_alias: 'lan_qwen', provider_type: 'ollama',
      provider_kind: 'self_hosted', host_scope: 'lan', compute_host: null,
    })).toBe('Extract Knowledge will send this conversation to lan_qwen (ollama, a machine on your network). Continue?');
  });

  it('names the host of the URL as the editor names a remembered entry', () => {
    const answer = {
      status: 'consent_required' as const, provider_alias: 'lan_qwen', provider_type: 'ollama',
      provider_kind: 'self_hosted', host_scope: 'lan', compute_host: null, url: 'http://192.168.1.5:11434',
    };
    expect(extractionConsentQuestion(answer))
      .toBe('Extract Knowledge will send this conversation to lan_qwen at 192.168.1.5:11434 (ollama, a machine on your network). Continue?');
    expect(rememberLabel(answer)).toBe("Don't ask again for lan_qwen at 192.168.1.5:11434");
    expect(rememberedEntryLine(answer).where.startsWith('192.168.1.5:11434 ')).toBe(true);
    expect(extractionConsentQuestion({ ...answer, url: 'localhost-box' })).toContain('lan_qwen at localhost-box (');
  });

  it('never calls a self-hosted engine elsewhere cloud', () => {
    expect(extractionConsentQuestion({
      status: 'consent_required', provider_alias: 'far_qwen', provider_type: 'ollama',
      provider_kind: 'self_hosted', host_scope: null, compute_host: null,
    })).toBe('Extract Knowledge will send this conversation to far_qwen (ollama, another machine). Continue?');
    expect(extractionConsentQuestion({
      status: 'consent_required', provider_alias: 'box', provider_type: 'ollama',
      provider_kind: 'self_hosted', host_scope: 'unknown', compute_host: null,
    })).toBe('Extract Knowledge will send this conversation to box (ollama, a host this node cannot place). Continue?');
  });

  it('does not say cloud for a generic server on an address of this machine', () => {
    expect(extractionConsentQuestion({
      status: 'consent_required', provider_alias: 'mine', provider_type: 'openai_compatible',
      provider_kind: 'vendor', host_scope: 'this_machine', compute_host: null,
    })).toBe('Extract Knowledge will send this conversation to mine (openai_compatible, a server at an address of this machine). Continue?');
  });

  it('names the peer by its display name', () => {
    expect(extractionConsentQuestion({
      status: 'consent_required', provider_alias: 'qwen', provider_type: null,
      provider_kind: 'self_hosted', compute_host: PEER,
    }, 'Linux')).toBe("Extract Knowledge will send this conversation to qwen on peer Linux (on that node's own hardware). Continue?");
  });

  it('falls back to a short node id when the peer has no name', () => {
    const text = extractionConsentQuestion({
      status: 'consent_required', provider_alias: 'qwen', provider_kind: 'vendor', compute_host: PEER,
    }, PEER);
    expect(text).toBe(`Extract Knowledge will send this conversation to qwen on peer ${PEER.slice(0, 20)} (cloud). Continue?`);
  });
});

describe('rememberLabel', () => {
  it('uses the name the question uses', () => {
    expect(rememberLabel({ status: 'consent_required', provider_alias: 'deepseek_flash', provider_type: 'deepseek', provider_kind: 'vendor' }))
      .toBe("Don't ask again for deepseek_flash");
    expect(rememberLabel({ status: 'consent_required', provider_alias: 'lan_qwen', provider_type: 'ollama', provider_kind: 'self_hosted', host_scope: 'lan' }))
      .toBe("Don't ask again for lan_qwen");
    expect(rememberLabel({ status: 'consent_required' })).toBe("Don't ask again for an unnamed model");
  });

  it('names a peer target with its display name, or its short id', () => {
    const peerAnswer = { status: 'consent_required' as const, provider_alias: 'qwen', provider_kind: 'self_hosted', compute_host: PEER };
    expect(rememberLabel(peerAnswer, 'Linux')).toBe("Don't ask again for qwen on peer Linux");
    expect(rememberLabel(peerAnswer, PEER)).toBe(`Don't ask again for qwen on peer ${PEER.slice(0, 20)}`);
    expect(rememberLabel({ ...peerAnswer, provider_alias: null }, 'Linux')).toBe("Don't ask again for its default model on peer Linux");
  });

  it('is the question without its brackets and its tail', () => {
    const answer = { status: 'consent_required' as const, provider_alias: 'qwen', provider_kind: 'self_hosted', compute_host: PEER };
    expect(extractionConsentQuestion(answer, 'Linux')).toContain(rememberLabel(answer, 'Linux').replace("Don't ask again for ", ''));
  });
});

describe('consentedPayload', () => {
  it('echoes all six target fields the owner was asked about, the URL included, under extract_consent', () => {
    expect(consentedPayload('ai_chat_1', {
      status: 'consent_required', provider_alias: 'lan_qwen', provider_type: 'ollama',
      provider_kind: 'self_hosted', host_scope: 'lan', compute_host: null, url: 'http://192.168.1.5:11434',
    })).toEqual({
      conversation_id: 'ai_chat_1',
      extract_consent: {
        provider_alias: 'lan_qwen', provider_type: 'ollama', provider_kind: 'self_hosted',
        host_scope: 'lan', compute_host: null, url: 'http://192.168.1.5:11434',
      },
    });
    expect(Object.keys(consentedPayload('c', { status: 'consent_required' }).extract_consent))
      .toEqual([...CONSENT_TARGET_FIELDS]);
  });

  it('carries remember: true only when the box was ticked', () => {
    const answer = { status: 'consent_required' as const, provider_alias: 'x' };
    expect(consentedPayload('c', answer, true).remember).toBe(true);
    expect('remember' in consentedPayload('c', answer, false)).toBe(false);
    expect('remember' in consentedPayload('c', answer)).toBe(false);
  });

  it('puts nothing beside the six fields in the echo', () => {
    const answer: any = { status: 'consent_required', provider_alias: 'x', url: 'http://192.168.1.20:11434', model: 'm' };
    expect(Object.keys(consentedPayload('c', answer, true).extract_consent)).toEqual([...CONSENT_TARGET_FIELDS]);
    expect(consentedPayload('c', answer, true).extract_consent.url).toBe('http://192.168.1.20:11434');
  });

  it('sends null for a field the answer left out', () => {
    expect(consentedPayload('c', { status: 'consent_required', provider_alias: 'deepseek_flash' }).extract_consent)
      .toEqual({ provider_alias: 'deepseek_flash', provider_type: null, provider_kind: null, host_scope: null, compute_host: null, url: null });
  });
});

describe('requestExtraction', () => {
  const ASKED = { status: 'consent_required', provider_alias: 'deepseek_flash', provider_type: 'deepseek', provider_kind: 'vendor', host_scope: null, compute_host: null };
  const noPeer = () => null;
  const yes = async () => ({ proceed: true, remember: false });
  const no = async () => ({ proceed: false, remember: false });
  const yesAndRemember = async () => ({ proceed: true, remember: true });

  function sender(...answers: unknown[]) {
    const sent: Array<{ command: string; payload: any }> = [];
    const send = (command: string, payload: any) => {
      sent.push({ command, payload });
      const next = answers.shift();
      return next === false ? false : Promise.resolve(next);
    };
    return { send, sent };
  }

  it('starts without a question when the backend does not ask', async () => {
    const { send, sent } = sender({ status: 'success' });
    let asked = 0;
    expect(await requestExtraction('c', send, async () => { asked++; return { proceed: true, remember: false }; }, noPeer)).toEqual({ kind: 'running' });
    expect(asked).toBe(0);
    expect(sent).toHaveLength(1);
  });

  it('asks, and resends with the echoed target after a yes', async () => {
    const { send, sent } = sender(ASKED, { status: 'success' });
    const questions: string[] = [];
    const outcome = await requestExtraction('c', send, async (q) => { questions.push(q); return { proceed: true, remember: false }; }, noPeer);
    expect(outcome).toEqual({ kind: 'running' });
    expect(questions).toEqual(['Extract Knowledge will send this conversation to deepseek_flash (deepseek, cloud). Continue?']);
    expect(sent[1].payload.extract_consent).toEqual({
      provider_alias: 'deepseek_flash', provider_type: 'deepseek', provider_kind: 'vendor', host_scope: null, compute_host: null, url: null,
    });
  });

  it('hands the box its label, built from the same target and peer name', async () => {
    const { send } = sender({ ...ASKED, compute_host: PEER, provider_alias: 'qwen' }, { status: 'success' });
    const labels: string[] = [];
    await requestExtraction('c', send, async (_q, label) => { labels.push(label); return { proceed: true, remember: false }; }, () => 'Linux');
    expect(labels).toEqual(["Don't ask again for qwen on peer Linux"]);
  });

  it('sends remember: true in the resend when the box was ticked', async () => {
    const { send, sent } = sender(ASKED, { status: 'success' });
    expect(await requestExtraction('c', send, yesAndRemember, noPeer)).toEqual({ kind: 'running' });
    expect(sent[0].payload).toEqual({ conversation_id: 'c' });
    expect(sent[1].payload.remember).toBe(true);
    expect(sent[1].payload.extract_consent.provider_alias).toBe('deepseek_flash');
  });

  it('sends no remember when the box was left unticked', async () => {
    const { send, sent } = sender(ASKED, { status: 'success' });
    await requestExtraction('c', send, yes, noPeer);
    expect('remember' in sent[1].payload).toBe(false);
  });

  it('sends nothing more, remember included, on a no', async () => {
    const { send, sent } = sender(ASKED, { status: 'success' });
    expect(await requestExtraction('c', send, async () => ({ proceed: false, remember: true }), noPeer)).toEqual({ kind: 'declined' });
    expect(sent).toHaveLength(1);
  });

  it('names the peer by the display name the page knows', async () => {
    const { send } = sender({ ...ASKED, compute_host: PEER, provider_alias: 'qwen' }, { status: 'success' });
    const questions: string[] = [];
    await requestExtraction('c', send, async (q) => { questions.push(q); return { proceed: true, remember: false }; }, () => 'Linux');
    expect(questions[0]).toContain('on peer Linux');
  });

  it('is declined silently on a no, and sends nothing more', async () => {
    const { send, sent } = sender(ASKED, { status: 'success' });
    expect(await requestExtraction('c', send, no, noPeer)).toEqual({ kind: 'declined' });
    expect(sent).toHaveLength(1);
  });

  it('says so when the backend still asks after the second yes', async () => {
    const { send, sent } = sender(ASKED, ASKED, ASKED);
    const outcome = await requestExtraction('c', send, yes, noPeer);
    expect(outcome.kind).toBe('failed');
    expect((outcome as any).message).toContain('changed again');
    expect(sent).toHaveLength(3);
  });

  it('says so when the socket is closed, on the first send and on the resend', async () => {
    for (const answers of [[false], [ASKED, false]]) {
      const { send } = sender(...answers);
      const outcome = await requestExtraction('c', send, yes, noPeer);
      expect(outcome.kind).toBe('failed');
      expect((outcome as any).message).toContain('connection');
    }
  });

  it('carries the backend sentence of an error, or a plain one', async () => {
    expect(await requestExtraction('c', sender({ status: 'error', message: 'Wait for it.' }).send, yes, noPeer))
      .toEqual({ kind: 'failed', message: 'Wait for it.' });
    expect(await requestExtraction('c', sender({ status: 'error' }).send, yes, noPeer))
      .toEqual({ kind: 'failed', message: 'Knowledge extraction failed.' });
  });
});

describe('a press cancelled before the vote', () => {
  const yes = async () => ({ proceed: true, remember: false });
  const answering = (answer: unknown) => () => Promise.resolve(answer);

  it('is cancelled, not running and not a failure', async () => {
    const outcome = await requestExtraction('c', answering({ status: 'cancelled', message: 'Knowledge extraction was cancelled before the vote; nothing was proposed.' }), yes, () => null);
    expect(outcome).toEqual({ kind: 'cancelled' });
  });

  it('is cancelled after a yes too', async () => {
    const answers: unknown[] = [
      { status: 'consent_required', provider_alias: 'deepseek_flash', provider_type: 'deepseek', provider_kind: 'vendor', compute_host: null },
      { status: 'cancelled' },
    ];
    expect(await requestExtraction('c', () => Promise.resolve(answers.shift()), yes, () => null)).toEqual({ kind: 'cancelled' });
  });
});

describe('cancelOutcome (the answer to Cancel)', () => {
  it('leaves the state to the pressed command when the cancel is taken', () => {
    expect(cancelOutcome({ status: 'success', conversation_id: 'c' })).toEqual({ stop: false, message: null });
  });

  it('clears a stale state silently when nothing runs', () => {
    expect(cancelOutcome({ status: 'error', reason: 'extraction_not_running', message: 'No knowledge extraction is running.' }))
      .toEqual({ stop: true, message: null });
  });

  it('says a vote has already started, with the backend sentence', () => {
    expect(cancelOutcome({ status: 'error', reason: 'vote_in_progress', message: 'The vote has already started.' }))
      .toEqual({ stop: true, message: 'The vote has already started.' });
  });

  it('says so when the socket is closed and keeps the state', () => {
    const outcome = cancelOutcome(false);
    expect(outcome.stop).toBe(false);
    expect(outcome.message).toContain('connection');
  });

  it('says so when the answer is not one it knows, and keeps the state', () => {
    for (const answer of [true, null, {}]) {
      const outcome = cancelOutcome(answer);
      expect(outcome.stop).toBe(false);
      expect(outcome.message).toContain('may still be running');
    }
  });
});

describe('rememberedEntryLine', () => {
  it('shows the alias and the host of its URL with the kind', () => {
    expect(rememberedEntryLine({
      provider_alias: 'lan_qwen', provider_type: 'ollama', provider_kind: 'self_hosted',
      host_scope: 'lan', compute_host: null, url: 'http://192.168.1.20:11434',
    })).toEqual({ name: 'lan_qwen', where: "192.168.1.20:11434 (on that node's own hardware)" });
  });

  it('shows a vendor as cloud and a URL-less entry as the default endpoint', () => {
    expect(rememberedEntryLine({ provider_alias: 'deepseek_flash', provider_kind: 'vendor', url: null }))
      .toEqual({ name: 'deepseek_flash', where: 'default endpoint (cloud)' });
  });

  it('shows a peer by its display name, or its short id', () => {
    const entry = { provider_alias: 'qwen', provider_kind: 'self_hosted', compute_host: PEER };
    expect(rememberedEntryLine(entry, 'Linux').where).toBe("peer Linux (on that node's own hardware)");
    expect(rememberedEntryLine(entry, null).where).toContain(PEER.slice(0, 20));
    expect(rememberedEntryLine({ compute_host: PEER, provider_kind: 'vendor' }, 'Linux').name).toBe('its default model');
  });
});

describe('withCurrentRemembered (the editor Save)', () => {
  const LAN = { provider_alias: 'lan_qwen', provider_type: 'ollama', provider_kind: 'self_hosted', host_scope: 'lan', compute_host: null, url: 'http://192.168.1.20:11434' };
  const VENDOR = { provider_alias: 'deepseek_flash', provider_type: 'deepseek', provider_kind: 'vendor', host_scope: null, compute_host: null, url: null };
  const ON_PEER = { provider_alias: 'qwen', provider_type: null, provider_kind: 'self_hosted', host_scope: null, compute_host: PEER, url: null };

  type Rules = { knowledge_extraction?: { send_without_asking?: Record<string, string | null>[] } };
  const none: Rules = {};

  function disk(list: unknown) {
    const sent: string[] = [];
    const send = (command: string) => {
      sent.push(command);
      return Promise.resolve({ status: 'success', rules: { knowledge_extraction: { send_without_asking: list } } });
    };
    return { send, sent };
  }

  it('keeps a yes remembered after Edit was pressed', async () => {
    const edited = { hub: { x: 'allow' }, knowledge_extraction: { send_without_asking: [LAN] } };  // the copy taken at Edit
    const { send, sent } = disk([LAN, VENDOR]);  // VENDOR was remembered while the editor was open
    const payload = await withCurrentRemembered(edited, [], send);
    expect(sent).toEqual(['get_firewall_rules']);
    expect(payload?.knowledge_extraction?.send_without_asking).toEqual([LAN, VENDOR]);
    expect(payload?.hub).toEqual({ x: 'allow' });
  });

  it('applies the Remove of this edit session to the list on disk', async () => {
    const edited = { knowledge_extraction: { send_without_asking: [] } };
    const payload = await withCurrentRemembered(edited, [LAN], disk([LAN, VENDOR]).send);
    expect(payload?.knowledge_extraction?.send_without_asking).toEqual([VENDOR]);
  });

  it('removes a peer entry by alias and peer, whatever kind the disk copy carries', async () => {
    const payload = await withCurrentRemembered(none, [ON_PEER], disk([{ ...ON_PEER, provider_kind: 'unknown' }, VENDOR]).send);
    expect(payload?.knowledge_extraction?.send_without_asking).toEqual([VENDOR]);
  });

  it('does not remove a local entry whose URL differs from the one removed', async () => {
    const moved = { ...LAN, url: 'http://192.168.1.99:11434' };
    const payload = await withCurrentRemembered(none, [LAN], disk([moved]).send);
    expect(payload?.knowledge_extraction?.send_without_asking).toEqual([moved]);
  });

  it('sends an empty list for a malformed or missing section, and nothing at all when the rules cannot be read', async () => {
    expect((await withCurrentRemembered(none, [], disk('x').send))?.knowledge_extraction?.send_without_asking).toEqual([]);
    expect(await withCurrentRemembered(none, [], () => false)).toBeNull();
    expect(await withCurrentRemembered(none, [], () => Promise.resolve({ status: 'error' }))).toBeNull();
  });

  it('is what the editor Save sends', () => {
    const save = editor.slice(editor.indexOf('async function saveChanges'), editor.indexOf('async function close'));
    expect(save).toMatch(/withCurrentRemembered\(editedRules, removedRemembered, sendCommand\)/);
    expect(save).toMatch(/rules_dict: payload/);
    expect(editor).toMatch(/removedRemembered = \[\.\.\.removedRemembered, gone\]/);
  });
});
