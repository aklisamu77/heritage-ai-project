import 'dotenv/config';
import { readFileSync, readdirSync, writeFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import Anthropic from '@anthropic-ai/sdk';
import pg from 'pg';

const pagesDir = './output/pages';
const ocrDir = './output/ocr';
const bookId = Number(process.env.BOOK_ID || 1);
const MODEL = 'claude-haiku-4-5-20251001';

mkdirSync(ocrDir, { recursive: true });

if (!process.env.ANTHROPIC_API_KEY) {
  console.error('ANTHROPIC_API_KEY غير موجود في .env — أضفه أولًا (انظر .env.example) ثم أعد المحاولة.');
  process.exit(1);
}

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

const PROMPT = `استخرج النص العربي الموجود في هذه الصورة كما هو بالضبط، حرفًا بحرف، مع الحفاظ الكامل على التشكيل (الحركات).
الصورة هي صفحة من كتاب "الأربعين النووية" للإمام النووي.
أعد فقط النص المستخرج بدون أي مقدمة أو تعليق أو شرح. إن كان في الصفحة رقم حديث ظاهر (مثل "الحديث الأول")، أبقه كما هو داخل النص.`;

async function ocrPage(imagePath) {
  const imageBuffer = readFileSync(imagePath);
  const base64 = imageBuffer.toString('base64');

  const response = await anthropic.messages.create({
    model: MODEL,
    max_tokens: 2048,
    messages: [
      {
        role: 'user',
        content: [
          { type: 'image', source: { type: 'base64', media_type: 'image/png', data: base64 } },
          { type: 'text', text: PROMPT },
        ],
      },
    ],
  });

  return response.content
    .filter((b) => b.type === 'text')
    .map((b) => b.text)
    .join('\n')
    .trim();
}

async function main() {
  const files = readdirSync(pagesDir).filter((f) => f.endsWith('.png')).sort();
  if (files.length === 0) {
    console.error(`لا توجد صور في ${pagesDir}. شغّل أولًا: npm run pdf-to-images`);
    process.exit(1);
  }

  const dbClient = new pg.Client();
  let dbConnected = false;
  try {
    await dbClient.connect();
    dbConnected = true;
  } catch (err) {
    console.warn('تحذير: تعذّر الاتصال بقاعدة البيانات، سيتم حفظ النتائج في ملفات JSON فقط.', err.message);
  }

  for (const file of files) {
    const pageNumber = Number(file.match(/\d+/)[0]);
    const imagePath = path.join(pagesDir, file);
    console.log(`OCR صفحة ${pageNumber}...`);

    const text = await ocrPage(imagePath);
    const jsonPath = path.join(ocrDir, `page-${String(pageNumber).padStart(3, '0')}.json`);
    writeFileSync(jsonPath, JSON.stringify({ page_number: pageNumber, raw_text: text, model: MODEL }, null, 2), 'utf8');

    if (dbConnected) {
      await dbClient.query(
        `INSERT INTO raw_pages (book_id, page_number, raw_text, ocr_model)
         VALUES ($1, $2, $3, $4)
         ON CONFLICT (book_id, page_number) DO UPDATE SET raw_text = EXCLUDED.raw_text, ocr_model = EXCLUDED.ocr_model`,
        [bookId, pageNumber, text, MODEL]
      );
    }

    console.log(`✓ صفحة ${pageNumber}: ${text.length} حرف`);
  }

  if (dbConnected) await dbClient.end();
  console.log('انتهى OCR لجميع الصفحات.');
}

main().catch((err) => {
  console.error('فشل OCR:', err);
  process.exit(1);
});
