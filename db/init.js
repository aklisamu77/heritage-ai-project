import 'dotenv/config';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import pg from 'pg';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const targetDb = process.env.PGDATABASE || 'heritage_poc';

async function ensureDatabase() {
  const admin = new pg.Client({
    host: process.env.PGHOST,
    port: process.env.PGPORT,
    user: process.env.PGUSER,
    password: process.env.PGPASSWORD,
    database: 'postgres',
  });
  await admin.connect();
  const { rowCount } = await admin.query('SELECT 1 FROM pg_database WHERE datname = $1', [targetDb]);
  if (rowCount === 0) {
    await admin.query(`CREATE DATABASE "${targetDb}"`);
    console.log(`تم إنشاء قاعدة البيانات: ${targetDb}`);
  } else {
    console.log(`قاعدة البيانات موجودة مسبقًا: ${targetDb}`);
  }
  await admin.end();
}

async function applySchema() {
  const client = new pg.Client({
    host: process.env.PGHOST,
    port: process.env.PGPORT,
    user: process.env.PGUSER,
    password: process.env.PGPASSWORD,
    database: targetDb,
  });
  await client.connect();
  const sql = readFileSync(path.join(__dirname, 'schema.sql'), 'utf8');
  await client.query(sql);
  console.log('تم تطبيق schema.sql بنجاح.');

  const bookSlug = process.env.BOOK_SLUG || 'al-arbaeen-al-nawawiya';
  const { rows } = await client.query(
    `INSERT INTO books (slug, title) VALUES ($1, $2)
     ON CONFLICT (slug) DO UPDATE SET title = EXCLUDED.title
     RETURNING id`,
    [bookSlug, 'الأربعين النووية']
  );
  console.log(`book_id = ${rows[0].id} (تأكد أنه يطابق BOOK_ID في .env)`);

  await client.end();
}

await ensureDatabase();
await applySchema();
