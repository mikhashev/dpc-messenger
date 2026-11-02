import { describe, it, expect } from 'vitest';
import {
    STRATA_MODEL_DOWNLOAD,
    STRATA_MODEL_EXTRAS_LINE,
    completedToastText,
    modelTableSizeText,
    modelTableSpaceText,
    strataModelExtrasLine,
    withoutSupersededStrataModel,
} from './strataModelDownload';

describe('the extras line', () => {
    it('is said for the Strata model download only, and names both extras', () => {
        expect(strataModelExtrasLine(STRATA_MODEL_DOWNLOAD)).toBe(STRATA_MODEL_EXTRAS_LINE);
        expect(strataModelExtrasLine('strata')).toBeNull();
        expect(strataModelExtrasLine('BAAI/bge-m3')).toBeNull();
        expect(STRATA_MODEL_EXTRAS_LINE).toContain('MTP, ~5.2 GB');
        expect(STRATA_MODEL_EXTRAS_LINE).toContain('~0.9 GB');
    });
});

describe('a model_table size', () => {
    it('is Strata\'s rounded figure, said "about"', () => {
        expect(modelTableSizeText(83_600_000_000)).toBe("about 83.6 GB (Strata's figure, rounded)");
        expect(modelTableSpaceText(83_600_000_000)).toBe('downloads about 83.6 GB');
    });

    it('is nothing for the model itself when its files are taken in place', () => {
        expect(modelTableSizeText(0)).toContain('your own files are used where they are');
        expect(modelTableSizeText(0)).toContain('.done file beside it');
        expect(modelTableSpaceText(0)).toBe('downloads nothing for the model itself');
    });

    it('is unknown, not 0, when the backend sent none', () => {
        expect(modelTableSizeText(null)).toBe('Size unknown');
    });
});

describe('a Strata model request that is still waiting', () => {
    const queue = [
        { model_name: 'BAAI/bge-m3', id: 1 },
        { model_name: 'strata-model', id: 2 },
    ];

    it('is replaced by a newer request for a Strata model, whatever its choice', () => {
        expect(withoutSupersededStrataModel(queue, { model_name: 'strata-model' })).toEqual([queue[0]]);
    });

    it('is left alone by any other request', () => {
        expect(withoutSupersededStrataModel(queue, { model_name: 'strata' })).toEqual(queue);
    });
});

describe('the toast at the end of a download', () => {
    const info = {
        purpose: 'Voice transcription',
        strata_model: { family: 'qwen', model: 'IQ3_S', images: true, gguf_dir: null },
    };

    it('names the Strata model and its run config', () => {
        expect(completedToastText(info, { model_name: 'strata-model', run_config: 'D:/m/strata/run-configs/strata-iq3_s.json' }))
            .toBe('✅ qwen IQ3_S installed (D:/m/strata/run-configs/strata-iq3_s.json).');
        expect(completedToastText(info, { model_name: 'strata-model' })).toBe('✅ qwen IQ3_S installed.');
    });

    it('is the old sentence for any other download', () => {
        expect(completedToastText(info, { model_name: 'openai/whisper-large-v3-turbo' }))
            .toBe('✅ openai/whisper-large-v3-turbo downloaded. Voice transcription is now available.');
        expect(completedToastText(undefined, { model_name: 'strata' })).toBe('✅ strata downloaded. It is now available.');
    });
});
