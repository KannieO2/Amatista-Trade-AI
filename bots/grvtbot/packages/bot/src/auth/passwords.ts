// Password hashing using bcryptjs.
//
// Pure JS, no native binaries — survives `npm install` on any host
// without a build toolchain. cost=12 is roughly equivalent to ~250ms
// per hash on a modern CPU, which is the standard recommendation
// (high enough to slow brute force, low enough to keep login snappy).

import bcrypt from 'bcryptjs';

const COST = 12;

export async function hashPassword(plain: string): Promise<string> {
  return bcrypt.hash(plain, COST);
}

export async function verifyPassword(plain: string, hash: string): Promise<boolean> {
  return bcrypt.compare(plain, hash);
}
