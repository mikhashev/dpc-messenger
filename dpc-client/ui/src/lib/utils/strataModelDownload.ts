// A Strata model installed through the model download consent flow
// (model_name 'strata-model'). Unlike the other downloads it is the owner's own
// request, asked again for every choice, so the panel does not dedupe it by name;
// the dialog's wording for its size and extras is here.

import type { ModelDownloadCompletedEvent, ModelDownloadRequiredEvent } from '$lib/types';

export const STRATA_MODEL_DOWNLOAD = 'strata-model';

// Setup also fetches what is not on the disk yet, which the model's own figure
// does not count: stated beside the size, for this download only.
export const STRATA_MODEL_EXTRAS_LINE =
    'Setup may also fetch the draft layer (MTP, ~5.2 GB) and the picture reader (~0.9 GB) if they are not present yet.';

export function strataModelExtrasLine(modelName: string): string | null {
    return modelName === STRATA_MODEL_DOWNLOAD ? STRATA_MODEL_EXTRAS_LINE : null;
}

// A new request for a Strata model replaces one that is still waiting for an answer:
// the confirm carries no choice, and the backend runs the last one asked.
export function withoutSupersededStrataModel<T extends { model_name: string }>(queue: T[], incoming: { model_name: string }): T[] {
    return incoming.model_name === STRATA_MODEL_DOWNLOAD
        ? queue.filter((p) => p.model_name !== STRATA_MODEL_DOWNLOAD)
        : queue;
}

// 'model_table' sizes are Strata's rounded `download_gb`, shown "about"; 0 is the
// model's own files taken in place.
export function modelTableSizeText(sizeBytes: number | null): string {
    if (sizeBytes == null) return 'Size unknown';
    if (sizeBytes === 0) return 'Nothing for the model itself: your own files are used where they are. Setup marks each file it checked with a .done file beside it.';
    return `about ${(sizeBytes / 1e9).toFixed(1)} GB (Strata\'s figure, rounded)`;
}

// The same figure in the dialog's "Disk Space" line.
export function modelTableSpaceText(sizeBytes: number): string {
    return sizeBytes === 0 ? 'downloads nothing for the model itself' : `downloads about ${(sizeBytes / 1e9).toFixed(1)} GB`;
}

// The toast when a download ends: a Strata model names itself and its run config.
export function completedToastText(
    info: Pick<ModelDownloadRequiredEvent, 'purpose' | 'strata_model'> | undefined,
    done: ModelDownloadCompletedEvent
): string {
    if (done.model_name === STRATA_MODEL_DOWNLOAD && info?.strata_model) {
        const { family, model } = info.strata_model;
        return `✅ ${family} ${model} installed${done.run_config ? ` (${done.run_config})` : ''}.`;
    }
    return `✅ ${done.model_name} downloaded. ${info?.purpose || 'It'} is now available.`;
}
