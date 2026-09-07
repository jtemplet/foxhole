# Foxhole Foundation (Mail Store to Searchable Brain) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest the entire Apple Mail local store (`.emlx` files) into a local Postgres + pgvector database with locally computed embeddings, exposing hybrid (keyword + vector) search over full email history via a CLI.

**Architecture:** A Bun/TypeScript workspace at `brain/` inside the existing foxhole repo (the Apps Script classifier at the repo root is untouched). A scanner walks `~/Library/Mail/V*/`, strips the emlx wrapper, parses MIME with postal-mime, and upserts messages plus 300-word chunks into Postgres. An `Embedder` interface (Ollama implementation, `nomic-embed-text`, 768 dims) fills chunk vectors. Search fuses a BM25-style tsvector arm and an HNSW cosine arm with Reciprocal Rank Fusion (k=60), max-pooled per message. Patterns are borrowed from gbrain: content-hash dedup, provider-agnostic embedding gateway with asymmetric doc/query prefixes, per-message max-pool before LIMIT.

**Tech Stack:** Bun 1.3 (runtime + test runner), TypeScript, PostgreSQL 18 (Homebrew) + pgvector 0.8.5, postal-mime (MIME parsing), html-to-text, postgres.js (client), Ollama + nomic-embed-text (embeddings), launchd (periodic sync).

**Follow-on plans (not in this document):** (2) Q&A over mail history plus MCP server, (3) daily digest with 1-5 priority scoring and awaiting-reply detection. Both consume the schema and search built here.

---

## Prerequisites (user actions, cannot be automated)

These must be done by Jason before Task 8's live run. Tasks 1-7 run entirely on fixtures and the test database and do not need them.

1. **Grant Full Disk Access** so processes can read `~/Library/Mail`:
   System Settings → Privacy & Security → Full Disk Access → add your terminal app AND `/Users/jtempleton/.bun/bin/bun` (press Cmd+Shift+G in the file picker to type the path). Restart the terminal afterward.
2. **Install Ollama and the embedding model:**
   ```bash
   brew install ollama
   brew services start ollama
   ollama pull nomic-embed-text
   ```

## File Structure

```
brain/
  package.json              Workspace manifest, scripts
  tsconfig.json             TypeScript config for Bun
  schema.sql                Full DDL (idempotent, CREATE IF NOT EXISTS)
  src/
    config.ts               Env-var backed configuration, single source of defaults
    db.ts                   postgres.js connection factory
    emlx.ts                 emlx wrapper stripping + MIME parse to ParsedEmail
    chunker.ts              300-word / 50-overlap text chunker
    scanner.ts              Mail-store walk + ingest (messages + chunks)
    embedder.ts             Embedder interface + OllamaEmbedder + embedPending
    search.ts               Hybrid RRF search
    cli.ts                  Command dispatch: init | doctor | sync | embed | search | run
    commands/doctor.ts      Environment health checks
  test/
    helpers.ts              Test DB reset, emlx fixture builder, FakeEmbedder
    emlx.test.ts
    chunker.test.ts
    scanner.test.ts
    embedder.test.ts
    search.test.ts
launchd/
  com.jtempleton.foxhole.sync.plist
```

Responsibilities: `emlx.ts` owns format knowledge, `scanner.ts` owns filesystem + upsert, `embedder.ts` owns vector generation, `search.ts` owns retrieval. No file imports from `commands/` except `cli.ts`.

---

### Task 1: Workspace scaffold

**Files:**
- Create: `brain/package.json`
- Create: `brain/tsconfig.json`
- Create: `brain/src/config.ts`
- Create: `brain/src/db.ts`
- Modify: `.claspignore` (append one line)

- [ ] **Step 1: Create `brain/package.json`**

```json
{
  "name": "foxhole-brain",
  "private": true,
  "type": "module",
  "scripts": {
    "cli": "bun run src/cli.ts",
    "test": "bun test"
  },
  "dependencies": {
    "html-to-text": "^9.0.5",
    "postal-mime": "^2.4.3",
    "postgres": "^3.4.5"
  },
  "devDependencies": {
    "@types/bun": "^1.2.0",
    "@types/html-to-text": "^9.0.4"
  }
}
```

- [ ] **Step 2: Create `brain/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ESNext",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "skipLibCheck": true,
    "types": ["bun"],
    "noEmit": true
  },
  "include": ["src", "test"]
}
```

- [ ] **Step 3: Install dependencies**

Run: `cd /Users/jtempleton/Dev/foxhole/brain && bun install`
Expected: lockfile `bun.lock` created, 3 dependencies + 2 dev dependencies installed, exit 0.

- [ ] **Step 4: Create `brain/src/config.ts`**

```typescript
export const config = {
  databaseUrl: process.env.FOXHOLE_DATABASE_URL ?? 'postgres://localhost/foxhole',
  mailRoot: process.env.FOXHOLE_MAIL_ROOT ?? `${process.env.HOME}/Library/Mail`,
  ollamaUrl: process.env.FOXHOLE_OLLAMA_URL ?? 'http://localhost:11434',
  embedModel: process.env.FOXHOLE_EMBED_MODEL ?? 'nomic-embed-text',
  embedDims: 768,
};
```

- [ ] **Step 5: Create `brain/src/db.ts`**

```typescript
import postgres from 'postgres';
import { config } from './config';

export function connect(url: string = config.databaseUrl) {
  return postgres(url, { onnotice: () => {} });
}

export type Sql = ReturnType<typeof connect>;
```

- [ ] **Step 6: Keep clasp from uploading the new tree.** Append this line to `/Users/jtempleton/Dev/foxhole/.claspignore`:

```
brain/**
```

- [ ] **Step 7: Commit**

```bash
git add brain/package.json brain/tsconfig.json brain/bun.lock brain/src/config.ts brain/src/db.ts .claspignore
git commit -m "feat: scaffold brain workspace (Bun + postgres.js + postal-mime)"
```

---

### Task 2: Schema and init command

**Files:**
- Create: `brain/schema.sql`
- Create: `brain/src/cli.ts` (initial version, extended in later tasks)
- Create: `brain/test/helpers.ts` (initial version)
- Test: `brain/test/schema.test.ts`

- [ ] **Step 1: Create the databases** (idempotent; `|| true` tolerates re-runs)

```bash
createdb foxhole 2>/dev/null || true
createdb foxhole_test 2>/dev/null || true
```

Expected: exit 0. Verify with `psql -l | grep foxhole` showing both rows.

- [ ] **Step 2: Create `brain/schema.sql`**

Note: the RFC 5322 References header is stored in a column named `refs` because `references` is a SQL keyword.

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS messages (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_id TEXT,
  content_hash TEXT NOT NULL,
  account TEXT NOT NULL,
  mailboxes TEXT[] NOT NULL DEFAULT '{}',
  emlx_path TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  from_name TEXT,
  from_email TEXT,
  to_emails TEXT[] NOT NULL DEFAULT '{}',
  cc_emails TEXT[] NOT NULL DEFAULT '{}',
  date_sent TIMESTAMPTZ,
  in_reply_to TEXT,
  refs TEXT[] NOT NULL DEFAULT '{}',
  thread_key TEXT,
  body_text TEXT NOT NULL DEFAULT '',
  has_attachments BOOLEAN NOT NULL DEFAULT FALSE,
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS messages_message_id_key
  ON messages (message_id) WHERE message_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS messages_content_hash_key ON messages (content_hash);
CREATE INDEX IF NOT EXISTS messages_thread_key_idx ON messages (thread_key);
CREATE INDEX IF NOT EXISTS messages_date_sent_idx ON messages (date_sent);

CREATE TABLE IF NOT EXISTS chunks (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  message_pk BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  chunk_index INT NOT NULL,
  chunk_text TEXT NOT NULL,
  embedding vector(768),
  model TEXT,
  search_vector TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED,
  UNIQUE (message_pk, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_search_idx ON chunks USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_unembedded_idx ON chunks (id) WHERE embedding IS NULL;

CREATE TABLE IF NOT EXISTS files_seen (
  path TEXT PRIMARY KEY,
  mtime_ms BIGINT NOT NULL,
  message_pk BIGINT REFERENCES messages(id) ON DELETE SET NULL,
  seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 3: Create `brain/test/helpers.ts`** (fixture builder and FakeEmbedder are added here in later tasks; start with DB reset only)

```typescript
import { connect, type Sql } from '../src/db';

export const TEST_DB_URL = 'postgres://localhost/foxhole_test';

export async function freshTestDb(): Promise<Sql> {
  const sql = connect(TEST_DB_URL);
  await sql`DROP TABLE IF EXISTS files_seen, chunks, messages CASCADE`;
  const schema = await Bun.file(new URL('../schema.sql', import.meta.url)).text();
  await sql.unsafe(schema);
  return sql;
}
```

- [ ] **Step 4: Write the failing test** at `brain/test/schema.test.ts`

```typescript
import { test, expect, afterAll } from 'bun:test';
import { freshTestDb } from './helpers';
import type { Sql } from '../src/db';

let sql: Sql;
afterAll(async () => { await sql?.end(); });

test('schema creates messages, chunks, files_seen with pgvector', async () => {
  sql = await freshTestDb();
  const tables = await sql`
    SELECT table_name FROM information_schema.tables
    WHERE table_schema = 'public' ORDER BY table_name`;
  const names = tables.map(t => t.table_name);
  expect(names).toContain('messages');
  expect(names).toContain('chunks');
  expect(names).toContain('files_seen');
  const [ext] = await sql`SELECT extversion FROM pg_extension WHERE extname = 'vector'`;
  expect(ext.extversion).toBeTruthy();
});
```

- [ ] **Step 5: Run the test.** This task inverts the usual test-first order because the artifact under test is DDL, not behavior; the test exists as a regression guard for later schema edits.

Run: `cd brain && bun test test/schema.test.ts`
Expected: `1 pass, 0 fail`. A failure names the missing table or the DDL syntax error; fix `schema.sql`, not the test.

- [ ] **Step 6: Create `brain/src/cli.ts`** with the `init` command

```typescript
import { connect } from './db';

async function init() {
  const sql = connect();
  const schema = await Bun.file(new URL('../schema.sql', import.meta.url)).text();
  await sql.unsafe(schema);
  await sql.end();
  console.log('Schema applied.');
}

const [cmd] = Bun.argv.slice(2);

switch (cmd) {
  case 'init':
    await init();
    break;
  default:
    console.log('Usage: bun run src/cli.ts <init>');
    process.exit(cmd ? 1 : 0);
}
```

- [ ] **Step 7: Apply the schema to the real database**

Run: `cd brain && bun run src/cli.ts init`
Expected: `Schema applied.`

- [ ] **Step 8: Commit**

```bash
git add brain/schema.sql brain/src/cli.ts brain/test/helpers.ts brain/test/schema.test.ts
git commit -m "feat: postgres schema with pgvector and init command"
```

---

### Task 3: emlx parser

**Files:**
- Create: `brain/src/emlx.ts`
- Modify: `brain/test/helpers.ts` (add fixture builder)
- Test: `brain/test/emlx.test.ts`

Background an engineer needs: a `.emlx` file is Apple Mail's on-disk message format: line 1 is the byte length of the RFC 822 payload, then exactly that many bytes of raw message, then an XML plist of Mail-internal flags. `.partial.emlx` variants (attachments stored separately) have the same wrapper. Parsing the payload is standard MIME work, delegated to postal-mime.

- [ ] **Step 1: Add the fixture builder to `brain/test/helpers.ts`** (append)

```typescript
export function makeEmlx(rfc822: string): Uint8Array {
  const body = new TextEncoder().encode(rfc822);
  const head = new TextEncoder().encode(`${body.length}\n`);
  const plist = new TextEncoder().encode(
    '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>flags</key><integer>0</integer></dict></plist>\n'
  );
  const out = new Uint8Array(head.length + body.length + plist.length);
  out.set(head, 0);
  out.set(body, head.length);
  out.set(plist, head.length + body.length);
  return out;
}

export const SAMPLE_RFC822 = [
  'Message-ID: <abc123@example.com>',
  'In-Reply-To: <root@example.com>',
  'References: <root@example.com> <mid@example.com>',
  'From: Ada Lovelace <ada@example.com>',
  'To: jtemple77@gmail.com',
  'Cc: Charles Babbage <charles@example.com>',
  'Subject: Analytical Engine invoice',
  'Date: Mon, 17 Aug 2026 09:30:00 -0700',
  'Content-Type: text/plain; charset=utf-8',
  '',
  'Please find the invoice for the analytical engine attached.',
  'Total due: $4,200 by September 1.',
  '',
].join('\r\n');
```

- [ ] **Step 2: Write the failing test** at `brain/test/emlx.test.ts`

```typescript
import { test, expect } from 'bun:test';
import { makeEmlx, SAMPLE_RFC822 } from './helpers';
import { stripEmlxWrapper, parseEmlx, threadKey } from '../src/emlx';

test('stripEmlxWrapper returns exactly the RFC822 payload', () => {
  const payload = stripEmlxWrapper(makeEmlx(SAMPLE_RFC822));
  expect(new TextDecoder().decode(payload)).toBe(SAMPLE_RFC822);
});

test('stripEmlxWrapper rejects a file without a length prefix', () => {
  const bogus = new TextEncoder().encode('not an emlx file\nat all');
  expect(() => stripEmlxWrapper(bogus)).toThrow('length prefix');
});

test('parseEmlx extracts headers, body, and thread linkage', async () => {
  const parsed = await parseEmlx(makeEmlx(SAMPLE_RFC822));
  expect(parsed.messageId).toBe('<abc123@example.com>');
  expect(parsed.subject).toBe('Analytical Engine invoice');
  expect(parsed.fromName).toBe('Ada Lovelace');
  expect(parsed.fromEmail).toBe('ada@example.com');
  expect(parsed.toEmails).toEqual(['jtemple77@gmail.com']);
  expect(parsed.ccEmails).toEqual(['charles@example.com']);
  expect(parsed.inReplyTo).toBe('<root@example.com>');
  expect(parsed.references).toEqual(['<root@example.com>', '<mid@example.com>']);
  expect(parsed.bodyText).toContain('Total due: $4,200');
  expect(parsed.dateSent?.toISOString()).toBe('2026-08-17T16:30:00.000Z');
  expect(parsed.hasAttachments).toBe(false);
});

test('threadKey prefers first reference, then in-reply-to, then own id', async () => {
  const parsed = await parseEmlx(makeEmlx(SAMPLE_RFC822));
  expect(threadKey(parsed)).toBe('<root@example.com>');
  expect(threadKey({ ...parsed, references: [] })).toBe('<root@example.com>');
  expect(threadKey({ ...parsed, references: [], inReplyTo: null })).toBe('<abc123@example.com>');
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd brain && bun test test/emlx.test.ts`
Expected: FAIL with `Cannot find module '../src/emlx'`

- [ ] **Step 4: Create `brain/src/emlx.ts`**

```typescript
import PostalMime from 'postal-mime';
import { convert } from 'html-to-text';

export interface ParsedEmail {
  messageId: string | null;
  subject: string;
  fromName: string | null;
  fromEmail: string | null;
  toEmails: string[];
  ccEmails: string[];
  dateSent: Date | null;
  inReplyTo: string | null;
  references: string[];
  bodyText: string;
  hasAttachments: boolean;
}

export function stripEmlxWrapper(raw: Uint8Array): Uint8Array {
  const newline = raw.indexOf(0x0a);
  const lengthLine = newline > 0 ? new TextDecoder().decode(raw.slice(0, newline)).trim() : '';
  const length = Number.parseInt(lengthLine, 10);
  if (!/^\d+$/.test(lengthLine) || !Number.isFinite(length) || length <= 0) {
    throw new Error('Not an emlx file: missing length prefix');
  }
  return raw.slice(newline + 1, newline + 1 + length);
}

function addresses(list: { address?: string }[] | undefined): string[] {
  return (list ?? [])
    .map(a => a.address?.toLowerCase())
    .filter((a): a is string => Boolean(a));
}

export async function parseEmlx(raw: Uint8Array): Promise<ParsedEmail> {
  const email = await PostalMime.parse(stripEmlxWrapper(raw));
  const bodyText = (email.text ?? convert(email.html ?? '', { wordwrap: false })).trim();
  return {
    messageId: email.messageId ?? null,
    subject: email.subject ?? '',
    fromName: email.from?.name || null,
    fromEmail: email.from?.address?.toLowerCase() ?? null,
    toEmails: addresses(email.to),
    ccEmails: addresses(email.cc),
    dateSent: email.date ? new Date(email.date) : null,
    inReplyTo: email.inReplyTo?.trim() || null,
    references: (email.references ?? '').split(/\s+/).filter(r => r.length > 0),
    bodyText,
    hasAttachments: (email.attachments ?? []).length > 0,
  };
}

export function threadKey(e: ParsedEmail): string | null {
  return e.references[0] ?? e.inReplyTo ?? e.messageId;
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd brain && bun test test/emlx.test.ts`
Expected: `4 pass, 0 fail`. If the date assertion fails, print `parsed.dateSent` and check postal-mime's `date` field type (it returns an ISO string); adjust only the test's expected value if the timezone math is what differs, never the parser.

- [ ] **Step 6: Commit**

```bash
git add brain/src/emlx.ts brain/test/emlx.test.ts brain/test/helpers.ts
git commit -m "feat: emlx wrapper stripping and MIME parsing"
```

---

### Task 4: Chunker

**Files:**
- Create: `brain/src/chunker.ts`
- Test: `brain/test/chunker.test.ts`

- [ ] **Step 1: Write the failing test** at `brain/test/chunker.test.ts`

```typescript
import { test, expect } from 'bun:test';
import { chunkText } from '../src/chunker';

const words = (n: number) => Array.from({ length: n }, (_, i) => `w${i}`).join(' ');

test('short text yields a single chunk', () => {
  expect(chunkText('hello world')).toEqual(['hello world']);
});

test('empty text yields no chunks', () => {
  expect(chunkText('')).toEqual([]);
  expect(chunkText('   \n  ')).toEqual([]);
});

test('long text is split into 300-word chunks with 50-word overlap', () => {
  const chunks = chunkText(words(700));
  expect(chunks.length).toBe(3);
  expect(chunks[0].split(' ').length).toBe(300);
  // Overlap: chunk 2 starts 50 words before chunk 1 ended
  expect(chunks[1].split(' ')[0]).toBe('w250');
  expect(chunks[2].split(' ')[0]).toBe('w500');
});

test('every input word appears in some chunk', () => {
  const chunks = chunkText(words(1000));
  const seen = new Set(chunks.flatMap(c => c.split(' ')));
  expect(seen.size).toBe(1000);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd brain && bun test test/chunker.test.ts`
Expected: FAIL with `Cannot find module '../src/chunker'`

- [ ] **Step 3: Create `brain/src/chunker.ts`**

```typescript
export function chunkText(text: string, maxWords = 300, overlapWords = 50): string[] {
  const words = text.split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  if (words.length <= maxWords) return [words.join(' ')];
  const chunks: string[] = [];
  let start = 0;
  while (start < words.length) {
    const end = Math.min(start + maxWords, words.length);
    chunks.push(words.slice(start, end).join(' '));
    if (end === words.length) break;
    start = end - overlapWords;
  }
  return chunks;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd brain && bun test test/chunker.test.ts`
Expected: `4 pass, 0 fail`

- [ ] **Step 5: Commit**

```bash
git add brain/src/chunker.ts brain/test/chunker.test.ts
git commit -m "feat: word-window chunker (300/50)"
```

---

### Task 5: Mail store scanner and ingest

**Files:**
- Create: `brain/src/scanner.ts`
- Modify: `brain/src/cli.ts` (add `sync` command)
- Test: `brain/test/scanner.test.ts`

Background: real paths look like `~/Library/Mail/V10/<ACCOUNT-UUID>/INBOX.mbox/<UUID>/Data/1/Messages/12345.emlx`. The account is the directory directly under `V10`; the mailbox is the deepest path segment ending in `.mbox`. Gmail accounts duplicate messages across mailboxes (`INBOX` and `All Mail`), so dedup is by Message-ID first, content hash second, and a duplicate only appends its mailbox name.

- [ ] **Step 1: Write the failing test** at `brain/test/scanner.test.ts`

```typescript
import { test, expect, beforeAll, afterAll } from 'bun:test';
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { freshTestDb, makeEmlx, SAMPLE_RFC822 } from './helpers';
import { syncMailStore, storeLocation } from '../src/scanner';
import type { Sql } from '../src/db';

const ROOT = '/tmp/foxhole-test-mail';
let sql: Sql;

beforeAll(async () => {
  sql = await freshTestDb();
  rmSync(ROOT, { recursive: true, force: true });
  const inbox = `${ROOT}/V10/ACCT-1111/INBOX.mbox/D1/Data/Messages`;
  const allMail = `${ROOT}/V10/ACCT-1111/All Mail.mbox/D2/Data/Messages`;
  mkdirSync(inbox, { recursive: true });
  mkdirSync(allMail, { recursive: true });
  writeFileSync(`${inbox}/1.emlx`, makeEmlx(SAMPLE_RFC822));
  // Same message duplicated in All Mail (same Message-ID)
  writeFileSync(`${allMail}/2.emlx`, makeEmlx(SAMPLE_RFC822));
  // A second, distinct message
  writeFileSync(`${inbox}/3.emlx`, makeEmlx(SAMPLE_RFC822
    .replace('<abc123@example.com>', '<def456@example.com>')
    .replace('Analytical Engine invoice', 'Lunch on Tuesday?')));
});

afterAll(async () => {
  rmSync(ROOT, { recursive: true, force: true });
  await sql.end();
});

test('storeLocation extracts account and mailbox from a relative path', () => {
  expect(storeLocation('V10/ACCT-1111/INBOX.mbox/D1/Data/Messages/1.emlx'))
    .toEqual({ account: 'ACCT-1111', mailbox: 'INBOX' });
});

test('sync ingests messages, dedupes across mailboxes, chunks bodies', async () => {
  const stats = await syncMailStore(sql, ROOT);
  expect(stats.scanned).toBe(3);
  expect(stats.failed).toBe(0);

  const rows = await sql`SELECT * FROM messages ORDER BY id`;
  expect(rows.length).toBe(2);
  expect(rows[0].mailboxes.sort()).toEqual(['All Mail', 'INBOX']);
  expect(rows[0].thread_key).toBe('<root@example.com>');

  const [chunkCount] = await sql`SELECT count(*)::int AS n FROM chunks`;
  expect(chunkCount.n).toBe(2);
});

test('a second sync skips unchanged files', async () => {
  const stats = await syncMailStore(sql, ROOT);
  expect(stats.skipped).toBe(3);
  expect(stats.ingested).toBe(0);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd brain && bun test test/scanner.test.ts`
Expected: FAIL with `Cannot find module '../src/scanner'`

- [ ] **Step 3: Create `brain/src/scanner.ts`**

```typescript
import { Glob } from 'bun';
import { stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { config } from './config';
import type { Sql } from './db';
import { parseEmlx, stripEmlxWrapper, threadKey } from './emlx';
import { chunkText } from './chunker';

export interface SyncStats {
  scanned: number;
  ingested: number;
  skipped: number;
  failed: number;
}

export function storeLocation(rel: string): { account: string; mailbox: string } {
  const parts = rel.split('/');
  const account = parts[1] ?? 'unknown';
  const mbox = [...parts].reverse().find(p => p.endsWith('.mbox'));
  return { account, mailbox: mbox ? mbox.slice(0, -'.mbox'.length) : 'unknown' };
}

export async function ingestEmlx(sql: Sql, raw: Uint8Array, rel: string, path: string): Promise<number> {
  const parsed = await parseEmlx(raw);
  const hash = createHash('sha256').update(stripEmlxWrapper(raw)).digest('hex');
  const { account, mailbox } = storeLocation(rel);

  const existing = parsed.messageId
    ? await sql`SELECT id, mailboxes FROM messages WHERE message_id = ${parsed.messageId}`
    : await sql`SELECT id, mailboxes FROM messages WHERE content_hash = ${hash}`;

  if (existing.length > 0) {
    const row = existing[0];
    if (!row.mailboxes.includes(mailbox)) {
      await sql`UPDATE messages SET mailboxes = array_append(mailboxes, ${mailbox}) WHERE id = ${row.id}`;
    }
    return row.id;
  }

  const [inserted] = await sql`
    INSERT INTO messages (message_id, content_hash, account, mailboxes, emlx_path,
                          subject, from_name, from_email, to_emails, cc_emails,
                          date_sent, in_reply_to, refs, thread_key, body_text, has_attachments)
    VALUES (${parsed.messageId}, ${hash}, ${account}, ${sql.array([mailbox])}, ${path},
            ${parsed.subject}, ${parsed.fromName}, ${parsed.fromEmail},
            ${sql.array(parsed.toEmails)}, ${sql.array(parsed.ccEmails)},
            ${parsed.dateSent}, ${parsed.inReplyTo}, ${sql.array(parsed.references)},
            ${threadKey(parsed)}, ${parsed.bodyText}, ${parsed.hasAttachments})
    ON CONFLICT (content_hash) DO UPDATE SET emlx_path = EXCLUDED.emlx_path
    RETURNING id`;

  const chunks = chunkText(parsed.bodyText);
  for (let i = 0; i < chunks.length; i++) {
    await sql`
      INSERT INTO chunks (message_pk, chunk_index, chunk_text)
      VALUES (${inserted.id}, ${i}, ${chunks[i]})
      ON CONFLICT (message_pk, chunk_index) DO NOTHING`;
  }
  return inserted.id;
}

export async function syncMailStore(sql: Sql, mailRoot: string = config.mailRoot): Promise<SyncStats> {
  const glob = new Glob('V*/**/*.emlx');
  const stats: SyncStats = { scanned: 0, ingested: 0, skipped: 0, failed: 0 };

  for await (const rel of glob.scan({ cwd: mailRoot, followSymlinks: false })) {
    const path = `${mailRoot}/${rel}`;
    stats.scanned++;
    try {
      const mtimeMs = Math.round((await stat(path)).mtimeMs);
      const [seen] = await sql`SELECT mtime_ms FROM files_seen WHERE path = ${path}`;
      if (seen && Number(seen.mtime_ms) === mtimeMs) {
        stats.skipped++;
        continue;
      }
      const raw = new Uint8Array(await Bun.file(path).arrayBuffer());
      const pk = await ingestEmlx(sql, raw, rel, path);
      await sql`
        INSERT INTO files_seen (path, mtime_ms, message_pk)
        VALUES (${path}, ${mtimeMs}, ${pk})
        ON CONFLICT (path) DO UPDATE
          SET mtime_ms = EXCLUDED.mtime_ms, message_pk = EXCLUDED.message_pk, seen_at = now()`;
      stats.ingested++;
    } catch (err) {
      stats.failed++;
      console.error(`Failed ${rel}: ${(err as Error).message}`);
    }
  }
  return stats;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd brain && bun test test/scanner.test.ts`
Expected: `3 pass, 0 fail`

- [ ] **Step 5: Add the `sync` command to `brain/src/cli.ts`.** Replace the file with:

```typescript
import { connect } from './db';
import { syncMailStore } from './scanner';

async function init() {
  const sql = connect();
  const schema = await Bun.file(new URL('../schema.sql', import.meta.url)).text();
  await sql.unsafe(schema);
  await sql.end();
  console.log('Schema applied.');
}

async function sync() {
  const sql = connect();
  const stats = await syncMailStore(sql);
  await sql.end();
  console.log(`Sync: ${stats.scanned} scanned, ${stats.ingested} ingested, ${stats.skipped} skipped, ${stats.failed} failed.`);
}

const [cmd] = Bun.argv.slice(2);

switch (cmd) {
  case 'init':
    await init();
    break;
  case 'sync':
    await sync();
    break;
  default:
    console.log('Usage: bun run src/cli.ts <init|sync>');
    process.exit(cmd ? 1 : 0);
}
```

- [ ] **Step 6: Run the full suite**

Run: `cd brain && bun test`
Expected: all tests pass (schema 1, emlx 4, chunker 4, scanner 3: `12 pass, 0 fail`)

- [ ] **Step 7: Commit**

```bash
git add brain/src/scanner.ts brain/src/cli.ts brain/test/scanner.test.ts
git commit -m "feat: mail store scanner with cross-mailbox dedup and sync command"
```

---

### Task 6: Embedder and embed command

**Files:**
- Create: `brain/src/embedder.ts`
- Modify: `brain/src/cli.ts` (add `embed` command)
- Modify: `brain/test/helpers.ts` (add FakeEmbedder)
- Test: `brain/test/embedder.test.ts`

Design constraint carried from the decisions: local now, swappable later. Everything downstream depends only on the `Embedder` interface. `nomic-embed-text` is asymmetric: documents and queries need different prefixes, exactly like gbrain's `embedQuery` distinction. The embed input prepends Subject/From so sender and topic are in the vector, while `chunk_text` in the database stays canonical body text.

- [ ] **Step 1: Write the failing test** at `brain/test/embedder.test.ts`

```typescript
import { test, expect } from 'bun:test';
import { OllamaEmbedder } from '../src/embedder';

function stubFetch(capture: { body?: any }, embeddings: number[][]) {
  return (async (_url: any, init: any) => {
    capture.body = JSON.parse(init.body);
    return new Response(JSON.stringify({ embeddings }), { status: 200 });
  }) as typeof fetch;
}

const vec768 = () => new Array(768).fill(0.1);

test('embedDocs applies the document prefix', async () => {
  const capture: { body?: any } = {};
  const e = new OllamaEmbedder('nomic-embed-text', 768, 'http://stub', stubFetch(capture, [vec768()]));
  await e.embedDocs(['hello']);
  expect(capture.body.model).toBe('nomic-embed-text');
  expect(capture.body.input).toEqual(['search_document: hello']);
});

test('embedQuery applies the query prefix and unwraps the single vector', async () => {
  const capture: { body?: any } = {};
  const e = new OllamaEmbedder('nomic-embed-text', 768, 'http://stub', stubFetch(capture, [vec768()]));
  const v = await e.embedQuery('find invoices');
  expect(capture.body.input).toEqual(['search_query: find invoices']);
  expect(v.length).toBe(768);
});

test('a dimension mismatch throws instead of storing bad vectors', async () => {
  const e = new OllamaEmbedder('nomic-embed-text', 768, 'http://stub',
    stubFetch({}, [new Array(384).fill(0.1)]));
  await expect(e.embedDocs(['hello'])).rejects.toThrow('384');
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd brain && bun test test/embedder.test.ts`
Expected: FAIL with `Cannot find module '../src/embedder'`

- [ ] **Step 3: Create `brain/src/embedder.ts`**

```typescript
import { config } from './config';
import type { Sql } from './db';

export interface Embedder {
  readonly model: string;
  readonly dims: number;
  embedDocs(texts: string[]): Promise<number[][]>;
  embedQuery(text: string): Promise<number[]>;
}

const PREFIXES: Record<string, { doc: string; query: string }> = {
  'nomic-embed-text': { doc: 'search_document: ', query: 'search_query: ' },
};

export class OllamaEmbedder implements Embedder {
  constructor(
    readonly model: string = config.embedModel,
    readonly dims: number = config.embedDims,
    private baseUrl: string = config.ollamaUrl,
    private fetcher: typeof fetch = fetch,
  ) {}

  private prefixes() {
    return PREFIXES[this.model] ?? { doc: '', query: '' };
  }

  private async embed(inputs: string[]): Promise<number[][]> {
    const res = await this.fetcher(`${this.baseUrl}/api/embed`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ model: this.model, input: inputs }),
    });
    if (!res.ok) throw new Error(`Ollama embed failed: ${res.status} ${await res.text()}`);
    const data = (await res.json()) as { embeddings: number[][] };
    for (const e of data.embeddings) {
      if (e.length !== this.dims) {
        throw new Error(`Model returned ${e.length} dims, expected ${this.dims}. Wrong model or schema migration needed.`);
      }
    }
    return data.embeddings;
  }

  embedDocs(texts: string[]) {
    return this.embed(texts.map(t => this.prefixes().doc + t));
  }

  async embedQuery(text: string) {
    return (await this.embed([this.prefixes().query + text]))[0];
  }
}

export async function embedPending(sql: Sql, embedder: Embedder, batchSize = 32): Promise<number> {
  let total = 0;
  for (;;) {
    const rows = await sql`
      SELECT c.id, c.chunk_text, m.subject,
             coalesce(m.from_name, m.from_email, '') AS sender
      FROM chunks c JOIN messages m ON m.id = c.message_pk
      WHERE c.embedding IS NULL
      ORDER BY c.id
      LIMIT ${batchSize}`;
    if (rows.length === 0) return total;

    const inputs = rows.map(r => `Subject: ${r.subject}\nFrom: ${r.sender}\n\n${r.chunk_text}`);
    const vectors = await embedder.embedDocs(inputs);
    for (let i = 0; i < rows.length; i++) {
      await sql`
        UPDATE chunks SET embedding = ${JSON.stringify(vectors[i])}::vector, model = ${embedder.model}
        WHERE id = ${rows[i].id}`;
    }
    total += rows.length;
    if (total % 320 === 0) console.log(`Embedded ${total} chunks...`);
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd brain && bun test test/embedder.test.ts`
Expected: `3 pass, 0 fail`

- [ ] **Step 5: Add FakeEmbedder to `brain/test/helpers.ts`** (append). It is a real bag-of-words hashing embedder: deterministic, shared words move vectors closer, which is exactly what search tests need.

```typescript
import type { Embedder } from '../src/embedder';

export class FakeEmbedder implements Embedder {
  readonly model = 'fake';
  readonly dims = 768;

  private vec(text: string): number[] {
    const v = new Array(768).fill(0);
    for (const word of text.toLowerCase().split(/\W+/).filter(Boolean)) {
      let h = 0;
      for (const ch of word) h = (h * 31 + ch.charCodeAt(0)) % 768;
      v[h] += 1;
    }
    const norm = Math.sqrt(v.reduce((s, x) => s + x * x, 0)) || 1;
    return v.map(x => x / norm);
  }

  async embedDocs(texts: string[]) { return texts.map(t => this.vec(t)); }
  async embedQuery(text: string) { return this.vec(text); }
}
```

- [ ] **Step 6: Add the `embed` command to `brain/src/cli.ts`.** Add these lines (import at top, case in the switch, usage string updated to `<init|sync|embed>`):

```typescript
import { OllamaEmbedder, embedPending } from './embedder';
```

```typescript
  case 'embed': {
    const sql = connect();
    const n = await embedPending(sql, new OllamaEmbedder());
    await sql.end();
    console.log(`Embedded ${n} chunks.`);
    break;
  }
```

- [ ] **Step 7: Run the full suite**

Run: `cd brain && bun test`
Expected: `15 pass, 0 fail`

- [ ] **Step 8: Commit**

```bash
git add brain/src/embedder.ts brain/src/cli.ts brain/test/embedder.test.ts brain/test/helpers.ts
git commit -m "feat: Embedder interface, Ollama implementation, embed command"
```

---

### Task 7: Hybrid search

**Files:**
- Create: `brain/src/search.ts`
- Modify: `brain/src/cli.ts` (add `search` command)
- Test: `brain/test/search.test.ts`

Design carried from gbrain: keyword and vector arms each produce a per-message ranking (max-pooled over chunks BEFORE the limit, so a long email cannot flood results), then Reciprocal Rank Fusion with k=60 merges them.

- [ ] **Step 1: Write the failing test** at `brain/test/search.test.ts`

```typescript
import { test, expect, beforeAll, afterAll } from 'bun:test';
import { freshTestDb, makeEmlx, SAMPLE_RFC822, FakeEmbedder } from './helpers';
import { ingestEmlx } from '../src/scanner';
import { embedPending } from '../src/embedder';
import { hybridSearch } from '../src/search';
import type { Sql } from '../src/db';

let sql: Sql;
const embedder = new FakeEmbedder();

beforeAll(async () => {
  sql = await freshTestDb();
  const second = SAMPLE_RFC822
    .replace('<abc123@example.com>', '<def456@example.com>')
    .replace('Analytical Engine invoice', 'Lunch on Tuesday?')
    .replace('Please find the invoice for the analytical engine attached.', 'Want to grab tacos near the office?')
    .replace('Total due: $4,200 by September 1.', 'Around noon works for me.');
  await ingestEmlx(sql, makeEmlx(SAMPLE_RFC822), 'V10/A/INBOX.mbox/D/Data/Messages/1.emlx', '/tmp/1.emlx');
  await ingestEmlx(sql, makeEmlx(second), 'V10/A/INBOX.mbox/D/Data/Messages/2.emlx', '/tmp/2.emlx');
  await embedPending(sql, embedder);
});

afterAll(async () => { await sql.end(); });

test('embedPending fills every chunk vector', async () => {
  const [row] = await sql`SELECT count(*)::int AS n FROM chunks WHERE embedding IS NULL`;
  expect(row.n).toBe(0);
});

test('keyword match ranks the invoice email first', async () => {
  const results = await hybridSearch(sql, embedder, 'invoice analytical engine');
  expect(results.length).toBeGreaterThan(0);
  expect(results[0].subject).toBe('Analytical Engine invoice');
  expect(results[0].score).toBeGreaterThan(0);
});

test('a lunch query ranks the lunch email first', async () => {
  const results = await hybridSearch(sql, embedder, 'tacos lunch');
  expect(results[0].subject).toBe('Lunch on Tuesday?');
});

test('limit is respected', async () => {
  const results = await hybridSearch(sql, embedder, 'invoice', 1);
  expect(results.length).toBe(1);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd brain && bun test test/search.test.ts`
Expected: FAIL with `Cannot find module '../src/search'`

- [ ] **Step 3: Create `brain/src/search.ts`**

```typescript
import type { Sql } from './db';
import type { Embedder } from './embedder';

export interface SearchResult {
  id: number;
  subject: string;
  fromEmail: string | null;
  dateSent: Date | null;
  score: number;
  snippet: string;
}

export async function hybridSearch(
  sql: Sql,
  embedder: Embedder,
  query: string,
  limit = 10,
): Promise<SearchResult[]> {
  const qvec = JSON.stringify(await embedder.embedQuery(query));

  const rows = await sql`
    WITH kw AS (
      SELECT message_pk, ROW_NUMBER() OVER (ORDER BY rank DESC) AS rn
      FROM (
        SELECT c.message_pk,
               MAX(ts_rank(c.search_vector, websearch_to_tsquery('english', ${query}))) AS rank
        FROM chunks c
        WHERE c.search_vector @@ websearch_to_tsquery('english', ${query})
        GROUP BY c.message_pk
        ORDER BY rank DESC
        LIMIT 50
      ) k
    ),
    vec AS (
      SELECT message_pk, ROW_NUMBER() OVER (ORDER BY dist) AS rn
      FROM (
        SELECT message_pk, MIN(dist) AS dist
        FROM (
          SELECT c.message_pk, c.embedding <=> ${qvec}::vector AS dist
          FROM chunks c
          WHERE c.embedding IS NOT NULL
          ORDER BY dist
          LIMIT 200
        ) nearest
        GROUP BY message_pk
        ORDER BY dist
        LIMIT 50
      ) v
    ),
    fused AS (
      SELECT COALESCE(kw.message_pk, vec.message_pk) AS message_pk,
             COALESCE(1.0 / (60 + kw.rn), 0) + COALESCE(1.0 / (60 + vec.rn), 0) AS score
      FROM kw FULL OUTER JOIN vec USING (message_pk)
    )
    SELECT m.id, m.subject, m.from_email, m.date_sent,
           f.score::float8 AS score, LEFT(m.body_text, 200) AS snippet
    FROM fused f
    JOIN messages m ON m.id = f.message_pk
    ORDER BY f.score DESC
    LIMIT ${limit}`;

  return rows.map(r => ({
    id: Number(r.id),
    subject: r.subject,
    fromEmail: r.from_email,
    dateSent: r.date_sent,
    score: r.score,
    snippet: r.snippet,
  }));
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd brain && bun test test/search.test.ts`
Expected: `4 pass, 0 fail`

- [ ] **Step 5: Add the `search` command to `brain/src/cli.ts`.** Add the import and case (usage string becomes `<init|sync|embed|search "query">`):

```typescript
import { hybridSearch } from './search';
```

```typescript
  case 'search': {
    const query = Bun.argv.slice(3).join(' ');
    if (!query) {
      console.error('Usage: bun run src/cli.ts search "your query"');
      process.exit(1);
    }
    const sql = connect();
    const results = await hybridSearch(sql, new OllamaEmbedder(), query, 10);
    await sql.end();
    for (const r of results) {
      const date = r.dateSent ? r.dateSent.toISOString().slice(0, 10) : 'unknown';
      console.log(`${r.score.toFixed(4)}  ${date}  ${r.fromEmail ?? '?'}  ${r.subject}`);
      console.log(`        ${r.snippet.replace(/\s+/g, ' ').slice(0, 120)}`);
    }
    break;
  }
```

- [ ] **Step 6: Run the full suite**

Run: `cd brain && bun test`
Expected: `19 pass, 0 fail`

- [ ] **Step 7: Commit**

```bash
git add brain/src/search.ts brain/src/cli.ts brain/test/search.test.ts
git commit -m "feat: hybrid RRF search (keyword + vector) with search command"
```

---

### Task 8: Doctor, launchd schedule, and first live run

**Files:**
- Create: `brain/src/commands/doctor.ts`
- Modify: `brain/src/cli.ts` (add `doctor` and `run` commands)
- Create: `launchd/com.jtempleton.foxhole.sync.plist`
- Modify: `README.md` (new section)

- [ ] **Step 1: Create `brain/src/commands/doctor.ts`**

Doctor is deliberately untested by unit tests: every check is a thin probe of a real external system, and the checks compose no logic worth isolating. Its verification is Step 3's live run.

```typescript
import { readdirSync } from 'node:fs';
import { config } from '../config';
import { connect } from '../db';
import { OllamaEmbedder } from '../embedder';

interface Check { name: string; ok: boolean; detail: string }

async function checkMailStore(): Promise<Check> {
  try {
    const versions = readdirSync(config.mailRoot).filter(d => /^V\d+$/.test(d));
    return versions.length > 0
      ? { name: 'mail store', ok: true, detail: `${config.mailRoot} readable, versions: ${versions.join(', ')}` }
      : { name: 'mail store', ok: false, detail: `${config.mailRoot} readable but no V* directory found` };
  } catch {
    return {
      name: 'mail store', ok: false,
      detail: `Cannot read ${config.mailRoot}. Grant Full Disk Access to your terminal and to ${process.execPath} in System Settings, then restart the terminal.`,
    };
  }
}

async function checkDatabase(): Promise<Check> {
  try {
    const sql = connect();
    const [ext] = await sql`SELECT extversion FROM pg_extension WHERE extname = 'vector'`;
    await sql.end();
    return ext
      ? { name: 'postgres', ok: true, detail: `connected, pgvector ${ext.extversion}` }
      : { name: 'postgres', ok: false, detail: 'connected, but pgvector extension missing. Run: bun run src/cli.ts init' };
  } catch (err) {
    return { name: 'postgres', ok: false, detail: `cannot connect to ${config.databaseUrl}: ${(err as Error).message}` };
  }
}

async function checkOllama(): Promise<Check> {
  try {
    const probe = await new OllamaEmbedder().embedQuery('doctor probe');
    return { name: 'ollama', ok: true, detail: `${config.embedModel} returned ${probe.length} dims` };
  } catch (err) {
    return {
      name: 'ollama', ok: false,
      detail: `${(err as Error).message}. Is Ollama running (brew services start ollama) and the model pulled (ollama pull ${config.embedModel})?`,
    };
  }
}

export async function doctor(): Promise<boolean> {
  const checks = await Promise.all([checkMailStore(), checkDatabase(), checkOllama()]);
  for (const c of checks) {
    console.log(`${c.ok ? 'PASS' : 'FAIL'}  ${c.name}: ${c.detail}`);
  }
  return checks.every(c => c.ok);
}
```

- [ ] **Step 2: Add `doctor` and `run` to `brain/src/cli.ts`.** Final version of the dispatch:

```typescript
import { connect } from './db';
import { syncMailStore } from './scanner';
import { OllamaEmbedder, embedPending } from './embedder';
import { hybridSearch } from './search';
import { doctor } from './commands/doctor';

async function init() {
  const sql = connect();
  const schema = await Bun.file(new URL('../schema.sql', import.meta.url)).text();
  await sql.unsafe(schema);
  await sql.end();
  console.log('Schema applied.');
}

async function sync() {
  const sql = connect();
  const stats = await syncMailStore(sql);
  await sql.end();
  console.log(`Sync: ${stats.scanned} scanned, ${stats.ingested} ingested, ${stats.skipped} skipped, ${stats.failed} failed.`);
}

async function embed() {
  const sql = connect();
  const n = await embedPending(sql, new OllamaEmbedder());
  await sql.end();
  console.log(`Embedded ${n} chunks.`);
}

const [cmd] = Bun.argv.slice(2);

switch (cmd) {
  case 'init':
    await init();
    break;
  case 'doctor':
    process.exit((await doctor()) ? 0 : 1);
  case 'sync':
    await sync();
    break;
  case 'embed':
    await embed();
    break;
  case 'run':
    await sync();
    await embed();
    break;
  case 'search': {
    const query = Bun.argv.slice(3).join(' ');
    if (!query) {
      console.error('Usage: bun run src/cli.ts search "your query"');
      process.exit(1);
    }
    const sql = connect();
    const results = await hybridSearch(sql, new OllamaEmbedder(), query, 10);
    await sql.end();
    for (const r of results) {
      const date = r.dateSent ? r.dateSent.toISOString().slice(0, 10) : 'unknown';
      console.log(`${r.score.toFixed(4)}  ${date}  ${r.fromEmail ?? '?'}  ${r.subject}`);
      console.log(`        ${r.snippet.replace(/\s+/g, ' ').slice(0, 120)}`);
    }
    break;
  }
  default:
    console.log('Usage: bun run src/cli.ts <init|doctor|sync|embed|run|search "query">');
    process.exit(cmd ? 1 : 0);
}
```

- [ ] **Step 3: Run doctor** (requires the Prerequisites section to be done)

Run: `cd brain && bun run src/cli.ts doctor`
Expected: three `PASS` lines. If `mail store` FAILs, Full Disk Access has not taken effect: re-check System Settings and restart the terminal. Do not proceed to Step 4 until all three pass.

- [ ] **Step 4: First live sync** (long: full history walk; safe to interrupt and re-run, dedup makes it resumable)

Run: `cd brain && bun run src/cli.ts sync`
Expected: final line `Sync: N scanned, M ingested, ...` with N in the thousands-to-hundreds-of-thousands range. A nonzero `failed` count under 1% of scanned is acceptable (malformed messages exist in every real mailbox); each failure logs its path for later inspection.

- [ ] **Step 5: First embed run** (long on first run; resumable, it only processes `embedding IS NULL`)

Run: `cd brain && bun run src/cli.ts embed`
Expected: progress lines, final `Embedded N chunks.`

- [ ] **Step 6: Smoke-check search against real mail** (a check that runs the main path end to end)

Run: `cd brain && bun run src/cli.ts search "invoice"`
Expected: 10 ranked results that are plausibly invoice-related. Judge quality by eye; this is the baseline the follow-on plans build on.

- [ ] **Step 7: Create `launchd/com.jtempleton.foxhole.sync.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.jtempleton.foxhole.sync</string>
  <key>WorkingDirectory</key>
  <string>/Users/jtempleton/Dev/foxhole/brain</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/jtempleton/.bun/bin/bun</string>
    <string>run</string>
    <string>src/cli.ts</string>
    <string>run</string>
  </array>
  <key>StartInterval</key>
  <integer>1800</integer>
  <key>StandardOutPath</key>
  <string>/tmp/foxhole-sync.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/foxhole-sync.err</string>
</dict>
</plist>
```

- [ ] **Step 8: Install the launchd job**

```bash
cp launchd/com.jtempleton.foxhole.sync.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jtempleton.foxhole.sync.plist
launchctl kickstart gui/$(id -u)/com.jtempleton.foxhole.sync
```

Expected: exit 0; within a minute `/tmp/foxhole-sync.log` ends with a `Sync: ...` line. If the log shows the mail-store Full Disk Access error, add `/Users/jtempleton/.bun/bin/bun` to Full Disk Access (Prerequisite 1) and kickstart again.

- [ ] **Step 9: Document in `README.md`.** Append this section:

```markdown
## Foxhole Brain (local email intelligence)

The `brain/` directory holds a local pipeline that mirrors the entire Apple Mail
store into Postgres + pgvector with locally computed embeddings (Ollama,
nomic-embed-text). Nothing leaves the machine.

- `cd brain && bun run src/cli.ts doctor` checks Full Disk Access, Postgres, and Ollama
- `bun run src/cli.ts run` syncs new mail and embeds it (launchd runs this every 30 minutes)
- `bun run src/cli.ts search "query"` hybrid keyword + semantic search over full history

Setup details and design live in `docs/superpowers/plans/2026-08-18-foxhole-foundation.md`.
The Apps Script classifier above is independent and unchanged.
```

- [ ] **Step 10: Final check and commit**

Run: `cd brain && bun test`
Expected: `19 pass, 0 fail`

```bash
git add brain/src/commands/doctor.ts brain/src/cli.ts launchd/com.jtempleton.foxhole.sync.plist README.md
git commit -m "feat: doctor checks, run command, launchd schedule, docs"
```
