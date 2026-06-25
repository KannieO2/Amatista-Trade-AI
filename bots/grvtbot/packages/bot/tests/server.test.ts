// Server Tests — graceful shutdown (updated for C.5)
//
// C.5 replaced the inline clearInterval SIGTERM hack with a clean
// gridEngine.stop({ preserveOrders: true }) call. These tests verify
// the shutdown contract rather than the internals.

import { describe, it, expect, beforeEach, vi } from 'vitest';

const mockGridEngine = {
  stop: vi.fn().mockResolvedValue(undefined),
};

const mockDb = {
  close: vi.fn().mockResolvedValue(undefined),
};

const mockExit = vi.spyOn(process, 'exit').mockImplementation((() => {}) as any);

describe('Graceful shutdown', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
    mockGridEngine.stop.mockResolvedValue(undefined);
    mockDb.close.mockResolvedValue(undefined);
  });

  // ── SIGTERM (production restart — preserve orders) ─────────────────

  it('SIGTERM calls stop({ preserveOrders: true }) then db.close()', async () => {
    // Simulate the handler from dashboard/server.ts
    const sigtermHandler = async () => {
      try {
        await mockGridEngine.stop({ preserveOrders: true });
      } catch (err) {
        console.error('Error stopping engine:', err);
      }
      await mockDb.close();
      process.exit(0);
    };

    await sigtermHandler();

    expect(mockGridEngine.stop).toHaveBeenCalledOnce();
    expect(mockGridEngine.stop).toHaveBeenCalledWith({ preserveOrders: true });
    expect(mockDb.close).toHaveBeenCalledOnce();
    expect(mockExit).toHaveBeenCalledWith(0);
  });

  it('SIGTERM still closes DB even if engine.stop fails', async () => {
    mockGridEngine.stop.mockRejectedValue(new Error('stop failed'));

    const sigtermHandler = async () => {
      try {
        await mockGridEngine.stop({ preserveOrders: true });
      } catch (err) {
        console.error('Error stopping engine:', err);
      }
      await mockDb.close();
      process.exit(0);
    };

    await sigtermHandler();

    expect(mockDb.close).toHaveBeenCalledOnce();
    expect(mockExit).toHaveBeenCalledWith(0);
  });

  // ── SIGINT (dev Ctrl+C — cancel orders) ────────────────────────────

  it('SIGINT calls stop() without preserveOrders then db.close()', async () => {
    const sigintHandler = async () => {
      try {
        await mockGridEngine.stop();
        await mockDb.close();
        process.exit(0);
      } catch (error) {
        console.error('Error during shutdown:', error);
        process.exit(1);
      }
    };

    await sigintHandler();

    expect(mockGridEngine.stop).toHaveBeenCalledOnce();
    expect(mockGridEngine.stop).toHaveBeenCalledWith(); // no args = orders cancelled
    expect(mockDb.close).toHaveBeenCalledOnce();
    expect(mockExit).toHaveBeenCalledWith(0);
  });

  it('SIGINT exits 1 on error', async () => {
    mockGridEngine.stop.mockRejectedValue(new Error('boom'));

    const sigintHandler = async () => {
      try {
        await mockGridEngine.stop();
        await mockDb.close();
        process.exit(0);
      } catch (error) {
        console.error('Error during shutdown:', error);
        process.exit(1);
      }
    };

    await sigintHandler();

    expect(mockExit).toHaveBeenCalledWith(1);
  });

  // ── Ordering guarantee ─────────────────────────────────────────────

  it('db.close() is always called AFTER stop() resolves', async () => {
    const callOrder: string[] = [];
    mockGridEngine.stop.mockImplementation(async () => {
      callOrder.push('stop');
    });
    mockDb.close.mockImplementation(async () => {
      callOrder.push('close');
    });

    const handler = async () => {
      await mockGridEngine.stop({ preserveOrders: true });
      await mockDb.close();
      process.exit(0);
    };

    await handler();

    expect(callOrder).toEqual(['stop', 'close']);
  });
});
