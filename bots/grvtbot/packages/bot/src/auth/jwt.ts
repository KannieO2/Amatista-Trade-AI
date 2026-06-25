// JWT helpers — HS256, 24h expiry, signed with JWT_SECRET.
//
// JWT_SECRET MUST be set in env (32+ random characters). Rotating
// it invalidates every active session — users have to log in again.
// That's acceptable but mention it in the ops runbook before doing
// it on production.

import jwt from 'jsonwebtoken';

const ISSUER = 'grvt-grid';
const EXPIRES_IN_SECONDS = 24 * 60 * 60; // 24h

function getSecret(): string {
  const s = process.env.JWT_SECRET;
  if (!s || s.length < 32) {
    throw new Error(
      'JWT_SECRET env var is missing or too short (need 32+ chars). ' +
        'Generate one with: head -c 48 /dev/urandom | base64'
    );
  }
  return s;
}

export interface JwtPayload {
  userId: number;
}

export function signToken(userId: number): string {
  return jwt.sign({ userId }, getSecret(), {
    algorithm: 'HS256',
    issuer: ISSUER,
    expiresIn: EXPIRES_IN_SECONDS,
  });
}

export function verifyToken(token: string): JwtPayload | null {
  try {
    const decoded = jwt.verify(token, getSecret(), {
      algorithms: ['HS256'],
      issuer: ISSUER,
    });
    if (typeof decoded === 'object' && decoded !== null && 'userId' in decoded) {
      return { userId: (decoded as { userId: number }).userId };
    }
    return null;
  } catch {
    return null;
  }
}
