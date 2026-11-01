import { describe, it, expect } from 'vitest';
import {
    groupModelFiles, humanSize, mmprojAfterChoosing, modelFileLabel, projectorsBeside, TOP_FOLDER_LABEL,
    type ModelFileRow,
} from './modelFiles';

const row = (path: string, folder: string, over: Partial<ModelFileRow> = {}): ModelFileRow => ({
    path, folder, size_bytes: 1024 ** 3, architecture: 'qwen35', is_mmproj: false, shard_count: 1, ...over,
});

const FILES: ModelFileRow[] = [
    row('/m/top.gguf', ''),
    row('/m/a--b/proj.gguf', 'a--b', { architecture: 'clip', is_mmproj: true }),
    row('/m/a--b/model1.gguf', 'a--b'),
    row('/m/a--b/model2.gguf', 'a--b'),
    row('/m/c--d/Vision-f16.gguf', 'c--d', { architecture: 'clip', is_mmproj: true }),
    row('/m/c--d/p2.gguf', 'c--d', { architecture: 'clip', is_mmproj: true }),
    row('/m/c--d/model.gguf', 'c--d'),
    row('/m/only--proj/mmproj.gguf', 'only--proj', { architecture: 'clip', is_mmproj: true }),
    row('/m/e--f/broken.gguf', 'e--f', { architecture: null }),
];

describe('the model select is grouped by subfolder and leaves the projectors out', () => {
    it('groups in the order given, names the top folder, and drops a folder of projectors only', () => {
        const groups = groupModelFiles(FILES);
        expect(groups.map((g) => g.label)).toEqual([TOP_FOLDER_LABEL, 'a--b', 'c--d', 'e--f']);
        expect(groups[1].rows.map((r) => r.path)).toEqual(['/m/a--b/model1.gguf', '/m/a--b/model2.gguf']);
        expect(groups.flatMap((g) => g.rows).some((r) => r.is_mmproj)).toBe(false);
    });

    it('a file whose header could not be read is still offered as a model', () => {
        expect(groupModelFiles(FILES).flatMap((g) => g.rows).map((r) => r.path)).toContain('/m/e--f/broken.gguf');
    });

    it('nothing listed gives no groups', () => {
        expect(groupModelFiles([])).toEqual([]);
        expect(groupModelFiles(null)).toEqual([]);
        expect(groupModelFiles(undefined)).toEqual([]);
    });
});

describe('a human size and a label', () => {
    it('picks the unit and keeps one decimal below 100', () => {
        expect(humanSize(0)).toBe('0 B');
        expect(humanSize(1536)).toBe('1.5 KB');
        expect(humanSize(15534575072)).toBe('14.5 GB');
        expect(humanSize(931146432)).toBe('888 MB');
        expect(humanSize(-1)).toBe('');
    });

    it('the label has the file name, the size and the parts of a split model', () => {
        expect(modelFileLabel(row('C:\\m\\a\\x.gguf', 'a'))).toBe('x.gguf (1.0 GB)');
        expect(modelFileLabel(row('/m/a/x-00001-of-00003.gguf', 'a', { shard_count: 3 })))
            .toBe('x-00001-of-00003.gguf (1.0 GB, 3 parts)');
    });
});

describe('the projector that goes with a chosen model', () => {
    it('is the is_mmproj rows of the model folder, whatever they are called', () => {
        expect(projectorsBeside(FILES, '/m/a--b/model1.gguf').map((r) => r.path)).toEqual(['/m/a--b/proj.gguf']);
        expect(projectorsBeside(FILES, '/m/c--d/model.gguf').map((r) => r.path))
            .toEqual(['/m/c--d/Vision-f16.gguf', '/m/c--d/p2.gguf']);
        expect(projectorsBeside(FILES, '/m/top.gguf')).toEqual([]);
    });

    it('a path that is not a listed model has no neighbours', () => {
        expect(projectorsBeside(FILES, '/typed/by/hand.gguf')).toEqual([]);
        expect(projectorsBeside(FILES, '/m/a--b/proj.gguf')).toEqual([]);
    });

    it('exactly one is taken, none beside leaves the current value alone', () => {
        expect(mmprojAfterChoosing(FILES, '/m/a--b/model2.gguf', undefined)).toBe('/m/a--b/proj.gguf');
        expect(mmprojAfterChoosing(FILES, '/m/a--b/model2.gguf', '/old.gguf')).toBe('/m/a--b/proj.gguf');
        expect(mmprojAfterChoosing(FILES, '/m/top.gguf', '/keep.gguf')).toBe('/keep.gguf');
        expect(mmprojAfterChoosing(FILES, '/m/top.gguf', undefined)).toBeUndefined();
    });

    it('several are a choice: the current one stays only if it is among them', () => {
        expect(mmprojAfterChoosing(FILES, '/m/c--d/model.gguf', '/m/c--d/p2.gguf')).toBe('/m/c--d/p2.gguf');
        expect(mmprojAfterChoosing(FILES, '/m/c--d/model.gguf', '/other/folder/proj.gguf')).toBeUndefined();
        expect(mmprojAfterChoosing(FILES, '/m/c--d/model.gguf', undefined)).toBeUndefined();
    });
});
