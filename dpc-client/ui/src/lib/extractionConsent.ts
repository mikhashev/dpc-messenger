/**
 * The question Extract Knowledge asks before a conversation leaves this machine.
 *
 * The backend answers `end_conversation_session` with `status: "consent_required"`
 * when the extraction model is not proven to run here (a vendor, a peer, a kind it
 * cannot tell, or a local engine whose URL names another machine). The resend
 * carries `extract_consent`: the target the owner was asked about, echoed back. If
 * the target has changed meanwhile, the backend asks again.
 */

export type ConsentRequired = {
  status: 'consent_required';
  provider_alias?: string | null;
  provider_type?: string | null;
  provider_kind?: string | null;
  host_scope?: string | null;
  compute_host?: string | null;
  url?: string | null;
};

export function isConsentRequired(answer: unknown): answer is ConsentRequired {
  return !!answer && typeof answer === 'object' && (answer as { status?: unknown }).status === 'consent_required';
}

/**
 * The fields of a `consent_required` answer that name the target (backend
 * `CONSENT_TARGET_FIELDS`, kept equal by a backend test). `url` is the alias's
 * address, null for a peer; an entry of a remembered yes stores the same fields.
 */
export const CONSENT_TARGET_FIELDS = [
  'provider_alias', 'provider_type', 'provider_kind', 'host_scope', 'compute_host', 'url',
] as const;

/**
 * The resend's payload: the one place the request fields are named. Echoes all
 * six target fields, the URL included, so a host that moved asks again; `remember` (the "Don't ask again" box) is sent only when
 * it is true, so an unchecked box leaves the payload as it was.
 */
export function consentedPayload(conversationId: string, answer: ConsentRequired, remember = false) {
  const target: Record<string, string | null> = {};
  for (const field of CONSENT_TARGET_FIELDS) target[field] = answer[field] ?? null;
  return {
    conversation_id: conversationId,
    extract_consent: target,
    ...(remember ? { remember: true } : {}),
  };
}

/** The host (and port) of a target's URL; a value that is no URL is shown as stored. */
function urlHost(url: string | null | undefined): string {
  if (!url) return '';
  try { return new URL(url).host || url; } catch { return url; }
}

// The screen words `where` uses for provider_kind.
const KIND_WORD: Record<string, string> = { vendor: 'cloud', self_hosted: "on that node's own hardware", unknown: 'unknown' };

/** The target's name and the words in brackets after it, as the question says them. */
function targetWords(answer: ConsentRequired, peerName?: string | null): { name: string; detail: string } {
  const kind = KIND_WORD[answer.provider_kind ?? 'unknown'] ?? 'unknown';
  if (answer.compute_host) {
    const host = peerName && peerName !== answer.compute_host ? peerName : answer.compute_host.slice(0, 20);
    const model = answer.provider_alias || 'its default model';
    return { name: `${model} on peer ${host}`, detail: kind };
  }
  const alias = answer.provider_alias || 'an unnamed model';
  // The host is named as the editor names a remembered entry: a yes is bound to it.
  const host = urlHost(answer.url);
  const name = host ? `${alias} at ${host}` : alias;
  // Where the URL's host is outranks the type's kind, as in `where`.
  let place = kind;
  if (answer.host_scope === 'lan') place = 'a machine on your network';
  else if (answer.host_scope === 'unknown') place = 'a host this node cannot place';
  else if (answer.host_scope === 'this_machine') place = 'a server at an address of this machine';
  else if (answer.provider_kind === 'self_hosted') place = 'another machine';
  return { name, detail: answer.provider_type ? `${answer.provider_type}, ${place}` : place };
}

/** `peerName` is the display name of `compute_host`, when the UI knows one. */
export function extractionConsentQuestion(answer: ConsentRequired, peerName?: string | null): string {
  const { name, detail } = targetWords(answer, peerName);
  return `Extract Knowledge will send this conversation to ${name} (${detail}). Continue?`;
}

/** The box under the question: the same name the question uses. */
export function rememberLabel(answer: ConsentRequired, peerName?: string | null): string {
  return `Don't ask again for ${targetWords(answer, peerName).name}`;
}

/** How many times a changed target is asked about before the press gives up. */
export const MAX_CONSENT_QUESTIONS = 2;

export type ExtractionOutcome =
  | { kind: 'running' }                    // the backend took the command; events follow
  | { kind: 'declined' }                   // the owner said no: nothing to show
  | { kind: 'failed'; message: string };   // nothing was sent: say why

type Sender = (command: string, payload: Record<string, unknown>) => Promise<unknown> | boolean;

/** The owner's answer to the question: whether to go on, and whether the box was ticked. */
export type ConsentDecision = { proceed: boolean; remember: boolean };

/**
 * One press of Extract Knowledge: send, ask while the backend asks, resend with
 * the owner's yes. Every way out other than `running` and `declined` carries a
 * sentence for the failure dialog; a transport error is thrown to the caller.
 */
export async function requestExtraction(
  conversationId: string,
  send: Sender,
  ask: (question: string, rememberLabel: string) => Promise<ConsentDecision>,
  peerNameOf: (nodeId: string) => string | null,
): Promise<ExtractionOutcome> {
  let answer: any = await send('end_conversation_session', { conversation_id: conversationId });
  for (let asked = 0; isConsentRequired(answer) && asked < MAX_CONSENT_QUESTIONS; asked++) {
    const peerName = answer.compute_host ? peerNameOf(answer.compute_host) : null;
    const decision = await ask(extractionConsentQuestion(answer, peerName), rememberLabel(answer, peerName));
    if (!decision.proceed) return { kind: 'declined' };
    answer = await send('end_conversation_session', consentedPayload(conversationId, answer, decision.remember));
  }
  if (answer === false) {
    return { kind: 'failed', message: 'The connection to the backend is closed, so nothing was sent. Reconnect and try again.' };
  }
  if (isConsentRequired(answer)) {
    return { kind: 'failed', message: 'The extraction model changed again while you were being asked, so nothing was sent. Press Extract Knowledge once more.' };
  }
  if (answer?.status === 'error') {
    return { kind: 'failed', message: answer.message || 'Knowledge extraction failed.' };
  }
  return { kind: 'running' };
}

/** One entry of `knowledge_extraction.send_without_asking` in the rules (backend `CONSENT_TARGET_FIELDS`). */
export type RememberedEntry = Partial<Record<(typeof CONSENT_TARGET_FIELDS)[number], string | null>>;

/** The editor's line for a remembered yes: the model, and where it runs (host or peer, and its kind). */
export function rememberedEntryLine(entry: RememberedEntry, peerName?: string | null): { name: string; where: string } {
  const kind = KIND_WORD[entry.provider_kind ?? 'unknown'] ?? 'unknown';
  const name = entry.provider_alias || 'its default model';
  if (entry.compute_host) {
    const peer = peerName && peerName !== entry.compute_host ? peerName : entry.compute_host.slice(0, 20);
    return { name, where: `peer ${peer} (${kind})` };
  }
  return { name, where: `${urlHost(entry.url) || 'default endpoint'} (${kind})` };
}

/**
 * What a remembered entry is compared on, as the backend compares it
 * (`knowledge_routing._remembered_identity`): alias and node id for a peer,
 * every field for a local alias.
 */
function rememberedIdentity(entry: RememberedEntry): string {
  const fields = entry.compute_host ? (['provider_alias', 'compute_host'] as const) : CONSENT_TARGET_FIELDS;
  return JSON.stringify(fields.map((field) => entry[field] || null));
}

type RulesWithRemembered = { knowledge_extraction?: { send_without_asking?: RememberedEntry[] } };

/**
 * The editor's Save payload with the remembered list as it is on disk now, minus
 * the entries removed in this edit session. The editor's copy was taken at Edit,
 * so sending it would erase a yes remembered while the editor was open; the
 * backend keeps the disk section only when a payload lacks it, which would undo
 * the editor's Remove. Null when the rules cannot be read: the caller saves nothing.
 */
export async function withCurrentRemembered<T extends RulesWithRemembered>(
  edited: T,
  removed: RememberedEntry[],
  send: Sender,
): Promise<T | null> {
  const answer: any = await send('get_firewall_rules', {});
  if (!answer || answer.status !== 'success' || !answer.rules || typeof answer.rules !== 'object') return null;
  const onDisk = answer.rules.knowledge_extraction?.send_without_asking;
  const gone = new Set(removed.map(rememberedIdentity));
  const kept: RememberedEntry[] = Array.isArray(onDisk)
    ? onDisk.filter((entry) => !!entry && typeof entry === 'object' && !gone.has(rememberedIdentity(entry)))
    : [];
  return { ...edited, knowledge_extraction: { ...(edited.knowledge_extraction ?? {}), send_without_asking: kept } };
}
