// The model files folder's GGUFs (list_model_files) for the Add Provider and Edit
// forms: the model select grouped by subfolder, and the projector that goes with a
// chosen model. A projector is a row with is_mmproj (the header's
// general.architecture is "clip"), never a guess from the file name.

export interface ModelFileRow {
    path: string;
    folder: string;                 // subfolder name inside the model files folder, "" = directly in it
    size_bytes: number;
    architecture: string | null;    // null = the header could not be read
    is_mmproj: boolean;
    shard_count: number;
}

export interface ModelFileGroup {
    label: string;
    rows: ModelFileRow[];
}

/** The label of the group of files that sit directly in the model files folder. */
export const TOP_FOLDER_LABEL = '(model files folder itself)';

export function fileNameOf(path: string): string {
    return path.split(/[\\/]/).pop() ?? path;
}

export function humanSize(bytes: number): string {
    if (!Number.isFinite(bytes) || bytes < 0) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit += 1;
    }
    return `${unit === 0 ? value : value.toFixed(value >= 100 ? 0 : 1)} ${units[unit]}`;
}

/** One option's text: the file name, its size, and the part count of a split model. */
export function modelFileLabel(row: ModelFileRow): string {
    const parts = row.shard_count > 1 ? `, ${row.shard_count} parts` : '';
    return `${fileNameOf(row.path)} (${humanSize(row.size_bytes)}${parts})`;
}

/** The non-projector rows grouped by subfolder, in the order the rows came (the
 *  backend sorts them). A folder holding only projectors makes no group. */
export function groupModelFiles(rows: ModelFileRow[] | null | undefined): ModelFileGroup[] {
    const groups: ModelFileGroup[] = [];
    for (const row of rows ?? []) {
        if (row.is_mmproj) continue;
        const label = row.folder || TOP_FOLDER_LABEL;
        let group = groups.find((g) => g.label === label);
        if (!group) {
            group = { label, rows: [] };
            groups.push(group);
        }
        group.rows.push(row);
    }
    return groups;
}

/** The projectors in the same folder as the model at `modelPath`; empty when that
 *  path is not a listed row (a path typed by hand has no known neighbours). */
export function projectorsBeside(rows: ModelFileRow[] | null | undefined, modelPath: string): ModelFileRow[] {
    const model = (rows ?? []).find((r) => r.path === modelPath && !r.is_mmproj);
    if (!model) return [];
    return (rows ?? []).filter((r) => r.is_mmproj && r.folder === model.folder);
}

/** The mmproj value after a model is chosen from the list: exactly one projector in
 *  its folder is taken; several are offered as a select, so `current` stays only if it
 *  is one of them and is otherwise cleared (the select would show "none"); none beside
 *  leaves `current` as it was. */
export function mmprojAfterChoosing(
    rows: ModelFileRow[] | null | undefined, modelPath: string, current: string | undefined,
): string | undefined {
    const beside = projectorsBeside(rows, modelPath);
    if (beside.length === 1) return beside[0].path;
    if (beside.length > 1) return beside.some((r) => r.path === current) ? current : undefined;
    return current;
}
