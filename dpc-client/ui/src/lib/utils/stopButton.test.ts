import { describe, it, expect } from 'vitest';
import { pressAction, stopButtonLabel, stopButtonTitle, stopButtonDisabled } from './stopButton';

describe('stop button state machine', () => {
    it('idle shows Stop and a press asks for a stop', () => {
        expect(stopButtonLabel('idle')).toBe('Stop');
        expect(pressAction('idle')).toBe('stop');
    });

    it('a second press on a stopping run is a kill, and the button reads Kill', () => {
        expect(stopButtonLabel('stopping')).toBe('Kill');
        expect(stopButtonDisabled('stopping')).toBe(false);
        expect(pressAction('stopping')).toBe('kill');
        expect(stopButtonTitle('stopping', 'stopping…')).toMatch(/aborts the model call now/);
    });

    it('once killing, further presses are ignored', () => {
        expect(stopButtonLabel('killing')).toBe('Killing…');
        expect(stopButtonDisabled('killing')).toBe(true);
        expect(pressAction('killing')).toBe('ignore');
    });

    it('a miss or an error can be pressed again as a plain stop', () => {
        expect(pressAction('miss')).toBe('stop');
        expect(pressAction('error')).toBe('stop');
        expect(stopButtonTitle('idle', '')).toBe('Stop agent');
    });
});
