// React hook bindings on top of the WS singleton.

import { useEffect, useRef, useSyncExternalStore } from 'react';
import { wsClient, type WsMessage, type WsStatus } from './ws-client';

/**
 * Subscribe to a WS channel for the lifetime of the component.
 * The handler is captured by ref so re-renders don't re-subscribe.
 */
export function useWsChannel<T = unknown>(
  channel: string,
  handler: (msg: WsMessage<T>) => void
): void {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    const unsubscribe = wsClient.subscribe(channel, (msg) => {
      handlerRef.current(msg as WsMessage<T>);
    });
    return unsubscribe;
  }, [channel]);
}

/**
 * Subscribe to the global connection status. Re-renders the component when
 * status changes. Use this to drive header indicators / fallback UI.
 */
export function useWsStatus(): WsStatus {
  return useSyncExternalStore(
    (notify) => wsClient.onStatusChange(() => notify()),
    () => wsClient.getStatus(),
    () => 'closed' as WsStatus
  );
}
