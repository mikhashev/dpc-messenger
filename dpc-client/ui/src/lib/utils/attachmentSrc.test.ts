/**
 * A path that came from another node must not be handed to the asset
 * protocol: the request 403s and the thumbnail the record also carries is
 * never shown. The platform is passed in, so each case is deterministic.
 */

import { describe, it, expect } from 'vitest';
import { pickImageSrc, isForeignPath } from './attachmentSrc';

const convert = (p: string) => `asset://${p}`;
const THUMB = 'data:image/png;base64,AAAA';
const WIN_PATH = 'C:\\Users\\mike\\.dpc\\x.png';

describe('pickImageSrc', () => {
    it('converts a Windows path on Windows', () => {
        const a = { file_path: WIN_PATH, thumbnail: THUMB };
        expect(pickImageSrc(a, convert, { isWindows: true })).toBe(`asset://${WIN_PATH}`);
    });

    it('converts a POSIX path on Linux', () => {
        const a = { file_path: '/home/mike/.dpc/x.png', thumbnail: THUMB };
        expect(pickImageSrc(a, convert, { isWindows: false })).toBe('asset:///home/mike/.dpc/x.png');
    });

    it('uses the thumbnail for a /home/... path on Windows', () => {
        const a = { file_path: '/home/mike/.dpc/conversations/g/files/screenshots/p.png', thumbnail: THUMB };
        expect(pickImageSrc(a, convert, { isWindows: true })).toBe(THUMB);
    });

    it('uses the thumbnail for a /Users/... path on Windows', () => {
        const a = { file_path: '/Users/mike/.dpc/x.png', thumbnail: THUMB };
        expect(pickImageSrc(a, convert, { isWindows: true })).toBe(THUMB);
    });

    it('uses the thumbnail for a drive path on Linux', () => {
        const a = { file_path: WIN_PATH, thumbnail: THUMB };
        expect(pickImageSrc(a, convert, { isWindows: false })).toBe(THUMB);
    });

    it('uses the thumbnail when there is no file_path', () => {
        expect(pickImageSrc({ thumbnail: THUMB }, convert, { isWindows: true })).toBe(THUMB);
    });

    it('yields an empty src for a foreign path with no thumbnail', () => {
        const a = { file_path: '/home/mike/.dpc/x.png' };
        expect(pickImageSrc(a, convert, { isWindows: true })).toBe('');
    });

    it('yields an empty src when there is nothing at all', () => {
        expect(pickImageSrc({}, convert, { isWindows: false })).toBe('');
    });
});

describe('isForeignPath', () => {
    it('does not mistake a relative or unrelated path for foreign', () => {
        expect(isForeignPath('screenshots/p.png', true)).toBe(false);
        expect(isForeignPath('/tmp/x.png', true)).toBe(false);
        expect(isForeignPath('/home/mike/x.png', false)).toBe(false);
    });
});
