/**
 * The Model picker in the providers editor, fed by `query_provider_models`.
 *
 * The backend answers [{id, kind}] already sorted (chat first, each `-noreason`
 * twin right after its base). This groups the rows by kind for <optgroup>s and
 * names the twins, because on NeuralDeep `-noreason` is the only way thinking
 * turns off and the bare id does not say so.
 */

export type ProviderModel = { id: string; kind: string };
export type ProviderModelGroup = { kind: string; label: string; models: ProviderModel[] };

const KIND_LABELS: Record<string, string> = {
    chat: 'Chat',
    embedding: 'Embeddings',
    rerank: 'Rerankers',
    stt: 'Speech to text',
    other: 'Other',
};

const NOREASON_SUFFIX = '-noreason';

export function modelOptionLabel(model: ProviderModel): string {
    return model.id.toLowerCase().endsWith(NOREASON_SUFFIX) ? `${model.id} (no thinking)` : model.id;
}

/** Groups in the order the rows arrive; an unknown kind goes under its own name. */
export function groupModels(models: ProviderModel[] | null | undefined): ProviderModelGroup[] {
    const groups: ProviderModelGroup[] = [];
    for (const m of models ?? []) {
        let group = groups.find((g) => g.kind === m.kind);
        if (!group) {
            group = { kind: m.kind, label: KIND_LABELS[m.kind] ?? m.kind, models: [] };
            groups.push(group);
        }
        group.models.push(m);
    }
    return groups;
}
