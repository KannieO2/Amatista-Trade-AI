// Tiny in-memory TTL cache for hot GRVT API calls.
//
// The dashboard polls /api/balance, /api/prices, etc. on every page render
// (and once per WS-connected client). Without caching, every refresh hammers
// the GRVT API and wastes our rate limit budget. A 2-second TTL is enough to
// debounce dashboard polling without making the data feel stale to traders.
//
// This is intentionally minimal: no LRU eviction, no async locks, no metrics.
// If we need more we'll switch to lru-cache, but for the 6-8 distinct keys
// we cache, a Map is enough.

import { childLogger } from './logger.js';

const log = childLogger('cache');

interface CacheEntry<T> {
  value: T;
  expiresAt: number;
}

export class TtlCache {
  private store = new Map<string, CacheEntry<unknown>>();

  /**
   * Get the cached value for `key` if it exists AND hasn't expired.
   * Returns undefined on miss or expired entry.
   */
  get<T>(key: string): T | undefined {
    const entry = this.store.get(key);
    if (!entry) return undefined;
    if (entry.expiresAt < Date.now()) {
      this.store.delete(key);
      return undefined;
    }
    return entry.value as T;
  }

  /**
   * Set a value with a TTL in milliseconds.
   */
  set<T>(key: string, value: T, ttlMs: number): void {
    this.store.set(key, { value, expiresAt: Date.now() + ttlMs });
  }

  /**
   * `getOrFetch(key, ttlMs, fetcher)` — return cached value if fresh, otherwise
   * call `fetcher`, cache the result, and return it. The single most common
   * usage pattern, so it's worth a helper.
   *
   * If two callers race for the same missing key, both will currently call
   * `fetcher`. That's fine for our cache sizes — duplicate requests are cheap
   * compared to managing a Map<string, Promise<T>>.
   */
  async getOrFetch<T>(key: string, ttlMs: number, fetcher: () => Promise<T>): Promise<T> {
    const cached = this.get<T>(key);
    if (cached !== undefined) {
      log.debug({ key }, 'cache hit');
      return cached;
    }
    log.debug({ key }, 'cache miss');
    const value = await fetcher();
    this.set(key, value, ttlMs);
    return value;
  }

  /**
   * Drop a single key (e.g. after a mutation that invalidates it).
   */
  invalidate(key: string): void {
    this.store.delete(key);
  }

  /**
   * Drop all keys matching a prefix (e.g. invalidate all bot-42-* keys
   * after a bot update).
   */
  invalidatePrefix(prefix: string): void {
    for (const k of this.store.keys()) {
      if (k.startsWith(prefix)) this.store.delete(k);
    }
  }

  /**
   * Drop everything. Useful for tests.
   */
  clear(): void {
    this.store.clear();
  }

  size(): number {
    return this.store.size;
  }
}

// Singleton — there's only one process and one cache.
export const cache = new TtlCache();
