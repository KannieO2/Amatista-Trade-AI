// Same logger setup as the bot package — pretty in dev, JSON in prod.

import pino from 'pino';

const isDev = process.env.NODE_ENV !== 'production';

export const rootLogger = pino({
  level: process.env.LOG_LEVEL || (isDev ? 'debug' : 'info'),
  ...(isDev && {
    transport: {
      target: 'pino-pretty',
      options: {
        colorize: true,
        translateTime: 'HH:MM:ss.l',
        ignore: 'pid,hostname',
      },
    },
  }),
  base: null,
});

export function childLogger(name: string, extra: Record<string, unknown> = {}) {
  return rootLogger.child({ subsystem: name, ...extra });
}

export type Logger = pino.Logger;
