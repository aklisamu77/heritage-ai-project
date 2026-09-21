import 'dotenv/config';
import { readFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { createCanvas } from '@napi-rs/canvas';
import { getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs';

const pdfPath = process.env.BOOK_PDF_PATH || './books-pdf/الاربعين-النووية.pdf';
const outDir = './output/pages';
const DPI = 300;

mkdirSync(outDir, { recursive: true });

async function main() {
  const data = new Uint8Array(readFileSync(pdfPath));
  const doc = await getDocument({ data }).promise;
  console.log(`عدد الصفحات: ${doc.numPages}`);

  for (let i = 1; i <= doc.numPages; i++) {
    const page = await doc.getPage(i);
    const scale = DPI / 72;
    const viewport = page.getViewport({ scale });

    const canvas = createCanvas(Math.ceil(viewport.width), Math.ceil(viewport.height));
    const ctx = canvas.getContext('2d');

    await page.render({ canvasContext: ctx, viewport }).promise;

    const outPath = path.join(outDir, `page-${String(i).padStart(3, '0')}.png`);
    const buffer = await canvas.encode('png');
    await import('node:fs/promises').then((fs) => fs.writeFile(outPath, buffer));
    console.log(`✓ ${outPath}`);
  }

  console.log('انتهى تحويل PDF إلى صور.');
}

main().catch((err) => {
  console.error('فشل التحويل:', err);
  process.exit(1);
});
