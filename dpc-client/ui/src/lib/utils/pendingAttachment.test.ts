import { describe, it, expect } from 'vitest';
import {
    composerHasContent, fileSendFields, formatMegabytes, isAttachmentPlaceholderText, knownDurationSeconds,
    mimeTypeFromName,
    pendingFileFrom, pendingFileTypeLabel, playsInline, replacedAttachmentNotice,
} from './pendingAttachment';

describe('the pending file', () => {
    it('takes its name from a Windows or a POSIX path and guesses its type from the extension', () => {
        expect(pendingFileFrom('C:\\Users\\x\\Music\\Song One.MP3', 4_918_633)).toEqual({
            filePath: 'C:\\Users\\x\\Music\\Song One.MP3', fileName: 'Song One.MP3',
            sizeBytes: 4_918_633, mimeType: 'audio/mpeg',
        });
        expect(pendingFileFrom('/home/x/notes.md', 10).fileName).toBe('notes.md');
    });

    it('knows no type for an unknown or missing extension, and the chip then shows the extension', () => {
        expect(mimeTypeFromName('archive.xyz')).toBe('');
        expect(mimeTypeFromName('Makefile')).toBe('');
        expect(mimeTypeFromName('.bashrc')).toBe('');
        expect(pendingFileTypeLabel(pendingFileFrom('/a/archive.xyz', 1))).toBe('XYZ');
        expect(pendingFileTypeLabel(pendingFileFrom('/a/Makefile', 1))).toBe('file');
        expect(pendingFileTypeLabel(pendingFileFrom('/a/take.wav', 1))).toBe('audio/wav');
    });

    it('a size core could not read is 0, never negative or NaN', () => {
        expect(pendingFileFrom('/a/b.txt', NaN).sizeBytes).toBe(0);
        expect(formatMegabytes(-5)).toBe('0.00 MB');
        expect(formatMegabytes(4_918_633)).toBe('4.69 MB');
    });
});

describe('one attachment per message', () => {
    it('says which attachment the newer one replaced', () => {
        expect(replacedAttachmentNotice({ kind: 'image', name: 'paste_1.png' }, 'song.mp3'))
            .toBe('The pasted image paste_1.png was replaced by song.mp3: a message carries one attachment.');
        expect(replacedAttachmentNotice({ kind: 'file', name: 'song.mp3' }, 'paste_2.png'))
            .toBe('The file song.mp3 was replaced by paste_2.png: a message carries one attachment.');
    });

    it('Send has something to send with words, an attachment alone, or both; blank words alone are nothing', () => {
        expect(composerHasContent('hello', false)).toBe(true);
        expect(composerHasContent('', true)).toBe(true);
        expect(composerHasContent('caption', true)).toBe(true);
        expect(composerHasContent('  \n ', false)).toBe(false);
    });
});

describe('the file send fields', () => {
    it('carry the words with the file when there are some', () => {
        expect(fileSendFields('/a/song.mp3', 4000, '  listen @Ark \n')).toEqual({
            file_path: '/a/song.mp3', file_size_bytes: 4000, text: 'listen @Ark',
        });
    });

    it('leave the words out for a file alone, so core records it as before', () => {
        expect(fileSendFields('/a/song.mp3', 4000)).toEqual({ file_path: '/a/song.mp3', file_size_bytes: 4000 });
        expect(fileSendFields('/a/song.mp3', 4000, '   ')).not.toHaveProperty('text');
    });
});

describe('an audio file plays in the chat (ADR-051 D19)', () => {
    const kept = 'C:\\Users\\x\\.dpc\\conversations\\group-1\\files\\song.mp3';

    it('a file with an audio/* type and a copy here plays', () => {
        expect(playsInline({ type: 'file', mime_type: 'audio/mpeg', file_path: kept })).toBe(true);
        expect(playsInline({ type: 'file', mime_type: 'Audio/WAV', file_path: kept })).toBe(true);
    });

    it('anything else keeps the row: another type, no copy here, a voice or an image attachment', () => {
        expect(playsInline({ type: 'file', mime_type: 'application/pdf', file_path: kept })).toBe(false);
        expect(playsInline({ type: 'file', mime_type: 'audio/mpeg' })).toBe(false);
        expect(playsInline({ type: 'file', file_path: kept })).toBe(false);
        expect(playsInline({ type: 'voice', mime_type: 'audio/webm', file_path: kept })).toBe(false);
        expect(playsInline({ type: 'image', mime_type: 'image/png', file_path: kept })).toBe(false);
    });

    it('takes the length the record knows, else 0 so the player measures it', () => {
        expect(knownDurationSeconds({ duration_seconds: 182.4 })).toBe(182.4);
        expect(knownDurationSeconds({
            voice_metadata: { duration_seconds: 3.5, sample_rate: 48000, channels: 1, codec: 'opus', recorded_at: '' },
        })).toBe(3.5);
        expect(knownDurationSeconds({})).toBe(0);
        expect(knownDurationSeconds({ duration_seconds: 0 })).toBe(0);
        expect(knownDurationSeconds({ duration_seconds: NaN })).toBe(0);
    });
});

describe('the line core writes for an attachment sent without words is not shown again', () => {
    // A record written before 2026-11-19 (group-60aff6110f02, #142): the name is not ASCII.
    const song = [{ filename: 'Тысячи умов.mp3' }];

    it('hides "Sent file" and "Received file" naming an attachment of the message', () => {
        expect(isAttachmentPlaceholderText(`Sent file: ${song[0].filename} (4.69 MB)`, song)).toBe(true);
        expect(isAttachmentPlaceholderText(`Received file: ${song[0].filename} (4.69 MB)`, song)).toBe(true);
        expect(isAttachmentPlaceholderText('Sent screenshot: shot.png (0.0 MB)', [{ filename: 'shot.png' }])).toBe(true);
        expect(isAttachmentPlaceholderText('Received voice message: v.webm (12 MB)', [{ filename: 'v.webm' }])).toBe(true);
    });

    it('keeps words the person wrote, a line naming another file, and a message without attachments', () => {
        expect(isAttachmentPlaceholderText('listen to this', song)).toBe(false);
        expect(isAttachmentPlaceholderText('Sent file: other.mp3 (4.69 MB)', song)).toBe(false);
        expect(isAttachmentPlaceholderText(`Sent file: ${song[0].filename} (4.69 MB) and more words`, song)).toBe(false);
        expect(isAttachmentPlaceholderText(`Sent file: ${song[0].filename} (big MB)`, song)).toBe(false);
        expect(isAttachmentPlaceholderText('Sent file: a.mp3 (1.0 MB)', [])).toBe(false);
        expect(isAttachmentPlaceholderText('Sent file: a.mp3 (1.0 MB)', undefined)).toBe(false);
        expect(isAttachmentPlaceholderText('', song)).toBe(false);
    });

    it('a name with regex characters is compared as text', () => {
        const odd = [{ filename: 'a (b) [c].mp3' }];
        expect(isAttachmentPlaceholderText('Sent file: a (b) [c].mp3 (2.5 MB)', odd)).toBe(true);
        expect(isAttachmentPlaceholderText('Sent file: aXbX.mp3 (2.5 MB)', [{ filename: 'a.b.mp3' }])).toBe(false);
    });
});
