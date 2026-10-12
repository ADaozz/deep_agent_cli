import type { RunSseFrame } from './types';
export class SseParser {
    private buffer = '';
    constructor(private receive: (frame: RunSseFrame) => void) { }
    push(text: string) {
        this.buffer += text;
        let boundary: RegExpExecArray | null;
        while ((boundary = /\r?\n\r?\n/.exec(this.buffer))) {
            const block = this.buffer.slice(0, boundary.index);
            this.buffer = this.buffer.slice(boundary.index + boundary[0].length);
            let id = '', event = 'message';
            const data: string[] = [];
            for (const line of block.split(/\r?\n/)) {
                if (line.startsWith(':'))
                    continue;
                const colon = line.indexOf(':');
                const key = colon < 0 ? line : line.slice(0, colon);
                const value = (colon < 0 ? '' : line.slice(colon + 1)).replace(/^ /, '');
                if (key === 'id')
                    id = value;
                else if (key === 'event')
                    event = value;
                else if (key === 'data')
                    data.push(value);
            }
            if (!data.length)
                continue;
            if (!/^\d+$/.test(id))
                throw new Error('SSE event has invalid id');
            if (!Number.isSafeInteger(Number(id)))
                throw new Error('SSE event id exceeds safe integer range');
            this.receive({ id: Number(id), event, data: JSON.parse(data.join('\n')) as unknown });
        }
    }
}
