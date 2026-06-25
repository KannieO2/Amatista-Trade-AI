// WebSocket event bus for the v2 server.
//
// This is the single point through which the bot's internal state changes
// are broadcast to all connected dashboard clients.
//
// Architecture:
//
//   GridEngine (EventEmitter)
//        |
//        v
//   ws-dispatcher.ts (subscribes to engine events, calls bus.publish)
//        |
//        v
//   wsBus (this file: tracks subscribers per channel)
//        |
//        v
//   ws-server.ts (manages WebSocket clients, calls bus.subscribe on connect)
//        |
//        v
//   client (React app)
//
// Channels are NAMED so the client can subscribe to only the data it needs:
//
//   bot:42         — all updates for bot 42 (status, position, pnl, fills)
//   bot:42:fills   — only fills for bot 42 (high-frequency, may be opt-in)
//   bot:42:orders  — only order placement/cancellation
//   prices         — global price ticker updates (broadcast to everyone)
//   notifications  — system notifications (auth issues, drawdown alerts)
//
// Messages are structured as { type, channel, data, timestamp }. The client
// switches on `type` to update its TanStack Query cache.

import { childLogger } from './logger.js';

const log = childLogger('ws-bus');

export type WsMessage = {
  type: string;
  channel: string;
  data: unknown;
  timestamp: number;
};

type Subscriber = (msg: WsMessage) => void;

class WebSocketBus {
  /**
   * Map of channel name -> set of subscriber callbacks.
   * Each WS client registers one subscriber per channel it's listening to.
   */
  private subscribers = new Map<string, Set<Subscriber>>();

  subscribe(channel: string, callback: Subscriber): () => void {
    let set = this.subscribers.get(channel);
    if (!set) {
      set = new Set();
      this.subscribers.set(channel, set);
    }
    set.add(callback);
    log.debug({ channel, totalSubscribers: set.size }, 'subscribed');

    // Return unsubscribe function
    return () => {
      const s = this.subscribers.get(channel);
      if (!s) return;
      s.delete(callback);
      if (s.size === 0) this.subscribers.delete(channel);
      log.debug({ channel, remaining: s?.size ?? 0 }, 'unsubscribed');
    };
  }

  /**
   * Publish a message to a single channel. All subscribers of that channel
   * receive it. Errors in individual subscribers are caught and logged so
   * one bad client can't break the broadcast.
   */
  publish(channel: string, type: string, data: unknown): void {
    const subs = this.subscribers.get(channel);
    if (!subs || subs.size === 0) return;  // no listeners, drop silently

    const msg: WsMessage = {
      type,
      channel,
      data,
      timestamp: Date.now()
    };

    for (const cb of subs) {
      try {
        cb(msg);
      } catch (err) {
        log.error({ err, channel, type }, 'subscriber callback threw');
      }
    }
  }

  /**
   * Publish to multiple channels in one call. Useful when an event is
   * relevant to several listeners (e.g. a fill goes to bot:42, bot:42:fills,
   * AND notifications).
   */
  publishToMany(channels: string[], type: string, data: unknown): void {
    for (const c of channels) this.publish(c, type, data);
  }

  /**
   * Number of currently-subscribed channels (for /api/health metrics).
   */
  channelCount(): number {
    return this.subscribers.size;
  }

  /**
   * Total number of active subscriptions across all channels.
   */
  subscriberCount(): number {
    let total = 0;
    for (const set of this.subscribers.values()) total += set.size;
    return total;
  }

  /**
   * Drop everything. For tests / shutdown.
   */
  clear(): void {
    this.subscribers.clear();
  }
}

export const wsBus = new WebSocketBus();
