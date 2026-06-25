// Singleton WebSocket connection manager.
//
// Mirrors the protocol of packages/bot/src/server/ws-server.ts:
//   - URL: ws[s]://host/ws?token=<jwt>
//   - Server sends `hello` on connect, `pong` on app-level ping
//   - Client subscribes to channels via { type: 'subscribe', channels: [...] }
//   - All frames JSON: { type, channel, data, timestamp }
//
// Auth: JWT-only. The legacy `?api_key=` mode was removed because the
// shared dashboard key was being shipped in the public Vite bundle. Server
// still accepts `?api_key=` for operator scripts (curl/admin); the browser
// never does.
//
// Reconnection: exponential backoff, capped. The hook layer (use-ws-channel)
// re-issues subscribe frames after every reconnect so consumers don't have to
// know about disconnect events.

import { getAuthToken } from './api-client';

export type WsStatus = 'connecting' | 'open' | 'closed' | 'error';

export interface WsMessage<T = unknown> {
  type: string;
  channel: string;
  data: T;
  timestamp: number;
}

type Listener = (msg: WsMessage) => void;
type StatusListener = (status: WsStatus) => void;

const RECONNECT_DELAYS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000];

class WsClient {
  private ws: WebSocket | null = null;
  private status: WsStatus = 'closed';
  private channelListeners = new Map<string, Set<Listener>>();
  private statusListeners = new Set<StatusListener>();
  private reconnectAttempt = 0;
  private reconnectTimer: number | null = null;
  private intentionallyClosed = false;
  private appPingTimer: number | null = null;

  private buildUrl(token: string): string {
    const baseOverride = import.meta.env.VITE_API_BASE_URL ?? '';
    let wsBase: string;
    if (baseOverride) {
      wsBase = baseOverride.replace(/^http/, 'ws');
    } else if (typeof window !== 'undefined') {
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
      wsBase = `${proto}://${window.location.host}`;
    } else {
      wsBase = 'ws://localhost:3848';
    }
    return `${wsBase}/ws?token=${encodeURIComponent(token)}`;
  }

  /**
   * Lazy connect. Safe to call multiple times — only connects once.
   * No-op if there is no JWT (the user hasn't logged in yet).
   */
  connect(): void {
    if (this.ws) {
      const state = this.ws.readyState;
      // OPEN or CONNECTING: already healthy, nothing to do.
      if (state === WebSocket.OPEN || state === WebSocket.CONNECTING) return;
      // CLOSING or CLOSED: abandon the old socket reference so we can
      // build a fresh one. Without this, an immediate reconnect after
      // disconnect() (e.g. login after logout) would bail here while
      // the old socket finishes its close handshake.
      this.ws = null;
    }
    const token = getAuthToken();
    if (!token) {
      // Nothing to authenticate with. Stay closed until AuthProvider
      // calls connect() again after login.
      this.setStatus('closed');
      return;
    }
    this.intentionallyClosed = false;
    this.setStatus('connecting');

    try {
      this.ws = new WebSocket(this.buildUrl(token));
    } catch (err) {
      console.error('[ws] failed to construct WebSocket', err);
      this.setStatus('error');
      this.scheduleReconnect();
      return;
    }

    this.ws.addEventListener('open', () => {
      this.reconnectAttempt = 0;
      this.setStatus('open');
      // Re-subscribe to all known channels after reconnect.
      const channels = Array.from(this.channelListeners.keys());
      if (channels.length > 0) {
        this.send({ type: 'subscribe', channels });
      }
      // App-level ping every 25s to keep things lively (the server also
      // does protocol-level pings every 30s; this is belt-and-suspenders).
      this.startAppPing();
    });

    this.ws.addEventListener('message', (event) => {
      let msg: WsMessage;
      try {
        msg = JSON.parse(event.data as string) as WsMessage;
      } catch {
        console.warn('[ws] received non-JSON frame', event.data);
        return;
      }
      this.dispatch(msg);
    });

    this.ws.addEventListener('close', (event) => {
      this.stopAppPing();
      this.ws = null;
      if (this.intentionallyClosed) {
        this.setStatus('closed');
        return;
      }
      // 4401 = unauthorized, don't retry blindly
      if (event.code === 4401) {
        console.error('[ws] unauthorized — token rejected by server');
        this.setStatus('error');
        return;
      }
      this.setStatus('closed');
      this.scheduleReconnect();
    });

    this.ws.addEventListener('error', (err) => {
      console.warn('[ws] error', err);
      // The close handler will fire right after — let it handle reconnect.
    });
  }

  /**
   * Subscribe a listener to a channel. Returns unsubscribe.
   * Auto-connects if not already connected.
   */
  subscribe(channel: string, listener: Listener): () => void {
    let listeners = this.channelListeners.get(channel);
    const isFirstSubscriber = !listeners;
    if (!listeners) {
      listeners = new Set();
      this.channelListeners.set(channel, listeners);
    }
    listeners.add(listener);

    // Lazy connect on first subscription
    this.connect();

    // If we're already open, send subscribe immediately.
    // (If not open yet, the open handler will send it on connect.)
    if (isFirstSubscriber && this.status === 'open') {
      this.send({ type: 'subscribe', channels: [channel] });
    }

    return () => {
      const set = this.channelListeners.get(channel);
      if (!set) return;
      set.delete(listener);
      if (set.size === 0) {
        this.channelListeners.delete(channel);
        if (this.status === 'open') {
          this.send({ type: 'unsubscribe', channels: [channel] });
        }
      }
    };
  }

  onStatusChange(listener: StatusListener): () => void {
    this.statusListeners.add(listener);
    // Fire immediately with current status so consumers don't have to
    // separately read it on mount.
    listener(this.status);
    return () => {
      this.statusListeners.delete(listener);
    };
  }

  getStatus(): WsStatus {
    return this.status;
  }

  /**
   * Force-close. Used by tests / hot-reload.
   */
  disconnect(): void {
    this.intentionallyClosed = true;
    if (this.reconnectTimer != null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.stopAppPing();
    if (this.ws) {
      this.ws.close(1000, 'client disconnect');
    }
  }

  private dispatch(msg: WsMessage): void {
    // System frames (hello, subscribed, pong) — log but no fanout.
    if (msg.channel === 'system') {
      if (msg.type === 'hello') {
        console.info('[ws] hello', msg.data);
      }
      return;
    }
    const listeners = this.channelListeners.get(msg.channel);
    if (!listeners || listeners.size === 0) return;
    for (const listener of listeners) {
      try {
        listener(msg);
      } catch (err) {
        console.error('[ws] listener error', err);
      }
    }
  }

  private send(payload: unknown): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    try {
      this.ws.send(JSON.stringify(payload));
    } catch (err) {
      console.error('[ws] send failed', err);
    }
  }

  private setStatus(status: WsStatus): void {
    if (this.status === status) return;
    this.status = status;
    for (const listener of this.statusListeners) {
      try {
        listener(status);
      } catch (err) {
        console.error('[ws] status listener error', err);
      }
    }
  }

  private scheduleReconnect(): void {
    if (this.intentionallyClosed) return;
    if (this.reconnectTimer != null) return;
    const delay =
      RECONNECT_DELAYS[Math.min(this.reconnectAttempt, RECONNECT_DELAYS.length - 1)] ?? 30_000;
    this.reconnectAttempt += 1;
    console.info(`[ws] reconnecting in ${delay}ms (attempt ${this.reconnectAttempt})`);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private startAppPing(): void {
    this.stopAppPing();
    this.appPingTimer = window.setInterval(() => {
      this.send({ type: 'ping' });
    }, 25_000);
  }

  private stopAppPing(): void {
    if (this.appPingTimer != null) {
      window.clearInterval(this.appPingTimer);
      this.appPingTimer = null;
    }
  }
}

// Module-level singleton — one WS connection per browser tab.
export const wsClient = new WsClient();
