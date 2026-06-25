// Outbound email — currently used only for E.9 password reset.
//
// SMTP is OPTIONAL. F.5 was skipped (Telegram is the primary notifier),
// but password recovery needs an out-of-band channel that does not depend
// on the user already being logged in. If SMTP_HOST/USER/PASS are set,
// mails go out the wire. If not, we log the URL at WARN level and the
// admin must deliver it manually — this keeps self-host without SMTP
// functional rather than blocking signup-grade installs.

import nodemailer, { type Transporter } from 'nodemailer';
import { childLogger } from '../server/logger.js';

const log = childLogger('mailer');

let transporter: Transporter | null = null;

export function isMailerConfigured(): boolean {
  return !!(process.env.SMTP_HOST && process.env.SMTP_USER && process.env.SMTP_PASS);
}

function getTransporter(): Transporter {
  if (transporter) return transporter;
  transporter = nodemailer.createTransport({
    host: process.env.SMTP_HOST,
    port: Number(process.env.SMTP_PORT ?? 587),
    secure: process.env.SMTP_PORT === '465',
    auth: {
      user: process.env.SMTP_USER,
      pass: process.env.SMTP_PASS,
    },
  });
  return transporter;
}

export interface PasswordResetEmail {
  to: string;
  resetUrl: string;
  expiresInMinutes: number;
}

export async function sendPasswordResetEmail(params: PasswordResetEmail): Promise<void> {
  if (!isMailerConfigured()) {
    log.warn(
      { to: params.to, resetUrl: params.resetUrl, expiresInMinutes: params.expiresInMinutes },
      'SMTP not configured — password reset URL must be delivered manually'
    );
    return;
  }
  const from = process.env.SMTP_FROM || `noreply@${process.env.SMTP_HOST}`;
  await getTransporter().sendMail({
    from,
    to: params.to,
    subject: 'Reset your GRVT Grid password',
    text:
      `We received a request to reset the password for this account.\n\n` +
      `Click the link below to choose a new password:\n${params.resetUrl}\n\n` +
      `This link expires in ${params.expiresInMinutes} minutes and can be used only once. ` +
      `If you did not request this, you can ignore this email — your password will stay the same.`,
    html:
      `<p>We received a request to reset the password for this account.</p>` +
      `<p><a href="${params.resetUrl}">Reset your password</a></p>` +
      `<p>This link expires in ${params.expiresInMinutes} minutes and can be used only once. ` +
      `If you did not request this, you can ignore this email — your password will stay the same.</p>`,
  });
  log.info({ to: params.to }, 'password reset email sent');
}
