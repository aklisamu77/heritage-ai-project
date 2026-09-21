-- الأسبوع 1-3: الجداول الأساسية لاختبار إثبات المفهوم

CREATE TABLE IF NOT EXISTS books (
    id SERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    total_pages INT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw_pages (
    id SERIAL PRIMARY KEY,
    book_id INT NOT NULL REFERENCES books(id),
    page_number INT NOT NULL,
    hadith_number INT,
    raw_text TEXT NOT NULL,
    ocr_confidence TEXT,
    ocr_model TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (book_id, page_number)
);

CREATE TABLE IF NOT EXISTS knowledge_objects (
    id SERIAL PRIMARY KEY,
    book_id INT NOT NULL REFERENCES books(id),
    page_number INT,
    hadith_number INT,
    original_text TEXT,
    extracted_idea TEXT,
    topic TEXT,
    confidence NUMERIC,
    model_used TEXT,
    status TEXT DEFAULT 'experimental',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS relationships (
    id SERIAL PRIMARY KEY,
    source_object_id INT NOT NULL REFERENCES knowledge_objects(id),
    target_object_id INT NOT NULL REFERENCES knowledge_objects(id),
    relation_type TEXT NOT NULL,
    confidence NUMERIC,
    created_at TIMESTAMPTZ DEFAULT now()
);
