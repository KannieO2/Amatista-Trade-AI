// Minimal Telegram Bot API client. We don't need the SDK — just one
// endpoint (sendMessage) and we already have undici as a dep for fetch.
//
// Falls back to logging the message if credentials are missing, so the
// notifier can run in a "dry-run" mode for local development.

import { request } from 'undici';
import { childLogger } from './logger.js';

const log = childLogger('telegram');

interface TelegramConfig {
  botToken: string;
  chatId: string;
}

export class TelegramClient {
  private readonly enabled: boolean;
  private readonly config: TelegramConfig | null;

  constructor(botToken: string | undefined, chatId: string | undefined) {
    if (botToken && chatId) {
      this.enabled = true;
      this.config = { botToken, chatId };
      log.info('Telegram enabled');
    } else {
      this.enabled = false;
      this.config = null;
      log.warn('Telegram credentials missing — running in DRY-RUN mode');
    }
  }

  /**
   * Send a Markdown-formatted message. Truncates at 4000 chars (Telegram
   * limit is 4096) and silently swallows API errors so a transient
   * Telegram outage doesn't kill the worker loop.
   */
  async send(text: string): Promise<void> {
    const trimmed = text.length > 4000 ? text.slice(0, 3990) + '\n…' : text;

    if (!this.enabled || !this.config) {
      log.info({ text: trimmed }, '[dry-run] would send');
      return;
    }

    const url = `https://api.telegram.org/bot${this.config.botToken}/sendMessage`;
    try {
      const res = await request(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          chat_id: this.config.chatId,
          text: trimmed,
          parse_mode: 'Markdown',
          disable_web_page_preview: true,
        }),
      });
      if (res.statusCode >= 300) {
        const body = await res.body.text();
        log.warn({ status: res.statusCode, body }, 'telegram send failed');
      } else {
        // Drain the body so the connection can be reused.
        await res.body.dump();
      }
    } catch (err) {
      log.error({ err: (err as Error).message }, 'telegram request errored');
    }
  }
}
