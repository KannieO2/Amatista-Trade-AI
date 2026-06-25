// D.7 — Webhook client tests (F.3).
// Same pattern as telegram: mock undici, exercise dry-run + happy +
// error paths, plus the optional secret header.

import { describe, it, expect, vi, beforeEach } from 'vitest';

const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));

vi.mock('undici', () => ({ request: mockRequest }));

import { WebhookClient } from '../src/webhook';

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

describe('WebhookClient (D.7)', () => {
  it('dry-runs when URL is missing — no HTTP call', async () => {
    const c = new WebhookClient(undefined);
    await c.send({ type: 'fill', message: 'test' });
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it('POSTs to the configured URL with a JSON body + ts field', async () => {
    mockRequest.mockResolvedValue(okResponse(200));
    const c = new WebhookClient('https://hooks.example.com/abc');

    const before = Date.now();
    await c.send({ type: 'drawdown', botId: 42, pair: 'ETH', message: 'down 5%' });
    const after = Date.now();

    expect(mockRequest).toHaveBeenCalledTimes(1);
    const [url, opts] = mockRequest.mock.calls[0]!;
    expect(url).toBe('https://hooks.example.com/abc');
    expect(opts.method).toBe('POST');
    expect(opts.headers['content-type']).toBe('application/json');

    const body = JSON.parse(opts.body as string);
    expect(body.type).toBe('drawdown');
    expect(body.botId).toBe(42);
    expect(body.pair).toBe('ETH');
    expect(body.message).toBe('down 5%');
    expect(body.ts).toBeGreaterThanOrEqual(before);
    expect(body.ts).toBeLessThanOrEqual(after);
  });

  it('adds x-webhook-secret header when secret is configured', async () => {
    mockRequest.mockResolvedValue(okResponse(200));
    const c = new WebhookClient('https://hooks.example.com', 'shh-secret');
    await c.send({ type: 'fill', message: 'x' });
    const [, opts] = mockRequest.mock.calls[0]!;
    expect(opts.headers['x-webhook-secret']).toBe('shh-secret');
  });

  it('omits x-webhook-secret header when secret is missing', async () => {
    mockRequest.mockResolvedValue(okResponse(200));
    const c = new WebhookClient('https://hooks.example.com');
    await c.send({ type: 'fill', message: 'x' });
    const [, opts] = mockRequest.mock.calls[0]!;
    expect(opts.headers['x-webhook-secret']).toBeUndefined();
  });

  it('does not throw on non-2xx response', async () => {
    mockRequest.mockResolvedValue({
      statusCode: 500,
      body: { text: async () => 'oops', dump: async () => undefined },
    });
    const c = new WebhookClient('https://hooks.example.com');
    await expect(c.send({ type: 'fill', message: 'x' })).resolves.toBeUndefined();
  });

  it('does not throw on network failure', async () => {
    mockRequest.mockRejectedValue(new Error('timeout'));
    const c = new WebhookClient('https://hooks.example.com');
    await expect(c.send({ type: 'fill', message: 'x' })).resolves.toBeUndefined();
  });

  it('sets request + body timeouts (10s) to avoid hanging the worker loop', async () => {
    mockRequest.mockResolvedValue(okResponse(200));
    const c = new WebhookClient('https://hooks.example.com');
    await c.send({ type: 'fill', message: 'x' });
    const [, opts] = mockRequest.mock.calls[0]!;
    expect(opts.headersTimeout).toBe(10_000);
    expect(opts.bodyTimeout).toBe(10_000);
  });
});
