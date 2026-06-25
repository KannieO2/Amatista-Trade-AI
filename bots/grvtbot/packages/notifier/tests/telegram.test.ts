// D.7 — Telegram client tests.
// Mock undici's `request` so we exercise the dry-run path, the success
// path, and the error swallowing without making real HTTP calls.

import { describe, it, expect, vi, beforeEach } from 'vitest';

// Hoisted mock for undici.request — must be set up before importing the
// client so the SUT picks up the stub.
const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));

vi.mock('undici', () => ({
  request: mockRequest,
}));

// Imported after the mock is installed.
import { TelegramClient } from '../src/telegram';

beforeEach(() => {
  mockRequest.mockReset();
});

function okResponse(statusCode = 200) {
  return {
    statusCode,
    body: {
      text: async () => '',
      dump: async () => undefined,
    },
  };
}

describe('TelegramClient (D.7)', () => {
  it('runs in dry-run when bot token is missing — no HTTP call made', async () => {
    const c = new TelegramClient(undefined, '12345');
    await c.send('hello');
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it('runs in dry-run when chat id is missing', async () => {
    const c = new TelegramClient('token', undefined);
    await c.send('hello');
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it('POSTs to api.telegram.org with the right body shape when configured', async () => {
    mockRequest.mockResolvedValue(okResponse(200));
    const c = new TelegramClient('mytoken', '12345');
    await c.send('*hello*');

    expect(mockRequest).toHaveBeenCalledTimes(1);
    const [url, opts] = mockRequest.mock.calls[0]!;
    expect(url).toBe('https://api.telegram.org/botmytoken/sendMessage');
    expect(opts.method).toBe('POST');
    expect(opts.headers).toMatchObject({ 'content-type': 'application/json' });
    const body = JSON.parse(opts.body as string);
    expect(body).toEqual({
      chat_id: '12345',
      text: '*hello*',
      parse_mode: 'Markdown',
      disable_web_page_preview: true,
    });
  });

  it('truncates messages over 4000 chars to 3990 + "\\n…"', async () => {
    mockRequest.mockResolvedValue(okResponse(200));
    const c = new TelegramClient('t', 'c');
    const long = 'x'.repeat(4500);
    await c.send(long);

    const [, opts] = mockRequest.mock.calls[0]!;
    const body = JSON.parse(opts.body as string);
    expect(body.text.length).toBe(3990 + '\n…'.length);
    expect(body.text.endsWith('\n…')).toBe(true);
  });

  it('does not throw on non-2xx response (kept worker alive)', async () => {
    mockRequest.mockResolvedValue({
      statusCode: 429,
      body: { text: async () => 'rate limited', dump: async () => undefined },
    });
    const c = new TelegramClient('t', 'c');
    await expect(c.send('hi')).resolves.toBeUndefined();
  });

  it('does not throw when the request itself rejects (network down)', async () => {
    mockRequest.mockRejectedValue(new Error('ECONNREFUSED'));
    const c = new TelegramClient('t', 'c');
    await expect(c.send('hi')).resolves.toBeUndefined();
  });
});
