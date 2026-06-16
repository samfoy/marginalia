# Calibre Integration

Calibre is optional but strongly recommended. This page explains what it adds, how to set it up, and what happens without it.

---

## What Calibre enables

Without Calibre, marginalia generates Book Index data from the LLM's training knowledge — no book text involved. This works well for widely-known books, but:
- Entity extraction is less precise (no passage citations, approximate chapter positions)
- The RAG index can't be built (no text to embed), so `/recap`, `/wiki`, `/chat` have no prose grounding
- Series metadata (book order, series name) may be wrong or missing

With Calibre, marginalia:
- Extracts the **full EPUB text** and sends it to the LLM for entity extraction — more accurate characters, locations, terms, and timeline events with real `first_appearance_pct` values
- Builds the **RAG index** from the actual prose — all companion features are grounded in what you've actually read
- Reads **authoritative series metadata** from Calibre's database (not the often-stale EPUB tags)

---

## Setup

### 1. Install Calibre

Download from [calibre-ebook.com](https://calibre-ebook.com). The default library location on macOS is `~/Calibre Library/`.

### 2. Add your books

Calibre needs the EPUBs in its library. Drag-and-drop EPUB files into the Calibre window, or use **Add books** (⌘A). Calibre copies the files into its managed library structure.

### 3. Check the library path

marginalia looks for your Calibre library directory at `~/Calibre Library/` (the macOS default). Point `MARGINALIA_CALIBRE_DB` to the **library folder** (not the `.db` file inside it):

```bash
export MARGINALIA_CALIBRE_DB="/path/to/your/Calibre Library"
```

To find your library path in Calibre: the current library is shown in the **title bar**, or go to **Preferences → Miscellaneous → Show current library location**.

### 4. Verify

```bash
cd bridge && python3 -c "
from book_finder import find_epub
r = find_epub('Dune', 'Frank Herbert')
print(r or 'NOT FOUND')
"
```

If it returns a path dict with `epub_path`, Calibre is wired up correctly.

---

## How it works

When a book opens in KOReader, the plugin calls `/book-index/init` with the title and author. The bridge:

1. Queries `metadata.db` for a matching book (fuzzy title + author match)
2. Falls back to title-only if the author doesn't match (EPUB metadata is often stale)
3. Extracts the full EPUB text with `epub_extract.py` (strips HTML, preserves chapter structure)
4. Reads series info from `metadata.db` — authoritative over EPUB tags
5. Sends the text to the LLM for Book Index generation

If no match is found, it falls back to knowledge-only mode (logged as `[knowledge_only]` strategy).

---

## Series metadata

Calibre's `metadata.db` stores the correct series name and book number for each title. This matters for the series-aware RAG: marginalia uses the series index to determine which books you've "already finished" and includes their context in cross-book queries.

If your books show the wrong series or no series in marginalia, check Calibre:

1. Select the book in Calibre
2. Click **Edit metadata** (⌘E)
3. Check the **Series** field and **Series index**

Common issues:
- EPUB tags often have stale or wrong series info — Calibre's manually-edited metadata is the source of truth
- Books bought from stores sometimes have the series name in the title instead of the Series field

---

## Author name mismatches

marginalia first tries an exact author match, then retries with title-only. If a book still isn't found, the EPUB author tag (what KOReader reports) doesn't match what's in Calibre. Fix it in Calibre's metadata editor, or check what the EPUB reports:

```bash
cd bridge && python3 -c "
from epub_extract import extract_epub
content = extract_epub('/path/to/your/book.epub')
print('title:', content.title)
print('author:', content.author)
"
```

---

## Without Calibre

marginalia still works — it just uses knowledge-only mode for all books. The Book Index is generated from the model's training data. For well-known published books (novels, non-fiction bestsellers), this is often good enough for the Book Index browser and basic lookups.

What doesn't work without Calibre:
- RAG index (no text to embed) — `/recap`, `/wiki`, `/chat` return responses without prose grounding
- Exact `first_appearance_pct` values (LLM estimates them)
- Series-aware cross-book queries (no reliable series index)

The Book Index browser, basic Ask AI lookups, and Obsidian note saving all work without Calibre.
