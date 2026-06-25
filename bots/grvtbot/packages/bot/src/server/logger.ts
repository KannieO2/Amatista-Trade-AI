// Structured logger for the v2 server.
// Replaces the regrettable hundreds of console.log calls in src/dashboard/server.ts.
// Use named child loggers per subsystem so log lines have context (e.g.
// `{subsystem: 'engine', botId: 42}`).

import pino from 'pino';

const isDev = process.env.NODE_ENV !== 'production';

export const rootLogger = pino({
  level: process.env.LOG_LEVEL || (isDev ? 'debug' : 'info'),
  // Pretty in dev (human-readable colored output), JSON in production
  // (so it parses cleanly in journalctl / log shippers).
  ...(isDev && {
    transport: {
      target: 'pino-pretty',
      options: {
        colorize: true,
        translateTime: 'HH:MM:ss.l',
        ignore: 'pid,hostname'
      }
    }
  }),
  base: null  // suppress pid/hostname in every line (pino requires null, not undefined)
});

export function childLogger(subsystem: string, extra: Record<string, unknown> = {}) {
  return rootLogger.child({ subsystem, ...extra });
}

export type Logger = pino.Logger;
