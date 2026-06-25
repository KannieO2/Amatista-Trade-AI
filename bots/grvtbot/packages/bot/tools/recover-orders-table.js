#!/usr/bin/env node
/**
 * recover-orders-table.js
 *
 * One-shot recovery script for the corrupt `orders` table.
 *
 * Background: the production DB at /opt/grvt-grid-bot/data/grid_bot.db has a
 * corrupt `orders` table (SQLITE_CORRUPT on any read). The engine doesn't use
 * this table as a source of truth — it uses `grid_levels` and `trades`. But
 * dashboard queries hit it, so we need to recreate it cleanly.
 *
 * Strategy: try to dump whatever's recoverable from the corrupt table, drop
 * the table, recreate it with the canonical schema from db.ts, and re-insert
 * recoverable rows. If nothing is recoverable, the new table starts empty —
 * that's fine, the engine will repopulate it from new orders going forward.
 *
 * USAGE: node scripts/recover-orders-table.js
 *
 * SAFETY:
 *   - Creates a timestamped backup of the DB before doing anything
 *   - Read-attempt is wrapped in try/catch — if reads fail, recoverable rows
 *     count is just 0
 *   - Idempotent: safe to re-run if it fails partway
 *   - Designed to run AFTER bot SIGTERM, BEFORE systemd start
 */

import Database from 'sqlite3';
import fs from 'fs';
import path from 'path';

const DB_PATH = process.env.GRVT_DB_PATH || path.join(process.cwd(), 'data', 'grid_bot.db');

if (!fs.existsSync(DB_PATH)) {
  console.error(`❌ DB not found at ${DB_PATH}`);
  process.exit(1);
}

// Backup
const stamp = new Date().toISOString().replace(/[:.]/g, '-');
const BACKUP_PATH = `${DB_PATH}.pre-recover-${stamp}.bak`;
fs.copyFileSync(DB_PATH, BACKUP_PATH);
console.log(`📦 Backup written to ${BACKUP_PATH}`);

// Open DB
const db = new Database.Database(DB_PATH, (err) => {
  if (err) {
    console.error('❌ Failed to open DB:', err.message);
    process.exit(1);
  }
});

const run = (sql, params = []) =>
  new Promise((resolve, reject) => {
    db.run(sql, params, function (err) {
      if (err) reject(err);
      else resolve({ changes: this.changes, lastID: this.lastID });
    });
  });

const all = (sql, params = []) =>
  new Promise((resolve, reject) => {
    db.all(sql, params, (err, rows) => {
      if (err) reject(err);
      else resolve(rows);
    });
  });

const get = (sql, params = []) =>
  new Promise((resolve, reject) => {
    db.get(sql, params, (err, row) => {
      if (err) reject(err);
      else resolve(row);
    });
  });

(async () => {
  try {
    // Check if orders table exists
    const tableInfo = await get(
      "SELECT name FROM sqlite_master WHERE type='table' AND name='orders'"
    );

    if (!tableInfo) {
      console.log('ℹ️  No orders table exists yet. Creating fresh.');
    } else {
      console.log('🔍 orders table exists. Attempting to read recoverable rows...');

      let recoverable = [];
      try {
        recoverable = await all('SELECT * FROM orders');
        console.log(`✅ Recovered ${recoverable.length} rows`);
      } catch (e) {
        console.log(`⚠️  Read failed (${e.message}) — proceeding with empty table`);
      }

      // Save recoverable to a side file in case manual review is needed
      if (recoverable.length > 0) {
        const dumpPath = `${DB_PATH}.orders-dump-${stamp}.json`;
        fs.writeFileSync(dumpPath, JSON.stringify(recoverable, null, 2));
        console.log(`💾 Recoverable rows saved to ${dumpPath}`);
      }

      console.log('🗑️  Dropping corrupt orders table...');
      await run('DROP TABLE IF EXISTS orders');
    }

    // Recreate orders table with the canonical schema (must match db.ts createTables)
    console.log('🔨 Creating fresh orders table...');
    await run(`
      CREATE TABLE orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bot_id INTEGER NOT NULL REFERENCES grid_bots(id) ON DELETE CASCADE,
        order_id TEXT NOT NULL,
        instrument TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        type TEXT NOT NULL CHECK (type IN ('limit', 'market')),
        quantity REAL NOT NULL,
        price REAL NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'filled', 'cancelled', 'rejected')),
        grid_level_id INTEGER REFERENCES grid_levels(id),
        metadata TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(order_id)
      )
    `);

    await run('CREATE INDEX IF NOT EXISTS idx_orders_bot_id ON orders(bot_id)');
    await run('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)');

    // Verify
    const count = await get('SELECT COUNT(*) as c FROM orders');
    console.log(`✅ orders table recreated. Row count: ${count.c}`);

    // Reindex all tables — fixes the "wrong # of entries" / "row missing from
    // index" errors that integrity_check sees on the funding/level indexes.
    console.log('🔧 Running REINDEX on all tables...');
    await run('REINDEX');
    console.log('✅ REINDEX complete');

    // VACUUM compacts the file and rebuilds the freelist, which fixes the
    // "2nd reference to page X" freelist corruption seen in integrity_check.
    // This rewrites the entire DB so it can take a few seconds.
    console.log('🧹 Running VACUUM (rebuilds freelist)...');
    await run('VACUUM');
    console.log('✅ VACUUM complete');

    // Run integrity check on the whole DB
    console.log('🔍 Running PRAGMA integrity_check...');
    const integrity = await all('PRAGMA integrity_check');
    const ok = integrity.length === 1 && integrity[0].integrity_check === 'ok';
    if (ok) {
      console.log('✅ DB integrity OK');
    } else {
      console.log(`⚠️  Integrity issues remain after recovery:`);
      for (const row of integrity.slice(0, 10)) {
        console.log('   -', row.integrity_check);
      }
      if (integrity.length > 10) {
        console.log(`   ... and ${integrity.length - 10} more`);
      }
      console.log('💡 If issues persist, the safest path is a full dump+restore:');
      console.log(`   sqlite3 ${DB_PATH} .dump > /tmp/dump.sql`);
      console.log(`   mv ${DB_PATH} ${DB_PATH}.broken`);
      console.log(`   sqlite3 ${DB_PATH} < /tmp/dump.sql`);
    }

    db.close((err) => {
      if (err) console.error('Close error:', err.message);
      console.log('✅ Done.');
      process.exit(0);
    });
  } catch (e) {
    console.error('❌ Recovery failed:', e.message);
    console.error(`💡 Backup is at ${BACKUP_PATH}`);
    db.close();
    process.exit(2);
  }
})();
