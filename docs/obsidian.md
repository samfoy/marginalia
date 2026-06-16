# Obsidian Integration

marginalia saves reading notes - highlighted passages, AI answers, and your own context - to your Obsidian vault. This page covers setup and the note structure.

---

## Setup

### Point marginalia at your vault

The easiest way is the setup wizard, which auto-detects vaults on your machine and lets you customise note folder locations:

```bash
marginalia setup
```

Or set the environment variables directly:

```bash
export MARGINALIA_VAULT=/path/to/your/obsidian/vault
# Optional (paths relative to vault, or absolute):
export MARGINALIA_BOOKS_DIR=Notes/Books       # per-book highlight/AI notes
export MARGINALIA_CAPTURES_DIR=Notes/Captures # standalone notes (Save as Note)
```

For a **macOS LaunchAgent** install, add to the plist's EnvironmentVariables section:

```xml
<key>MARGINALIA_VAULT</key>
<string>/Users/yourname/Documents/MyVault</string>
```

The vault root is the folder that contains your `.obsidian/` directory - the same path you'd use when opening Obsidian.

### Verify

After saving your first note from KOReader, files should appear at:

```
<MARGINALIA_VAULT>/Notes/Books/<Author> - <Title>.md   ← per-book notes
<MARGINALIA_VAULT>/Notes/Captures/<Note title>.md       ← standalone notes
```

---

## Note structure

Each book gets its own markdown file. The first time a note is saved, marginalia creates the file with YAML frontmatter:

```markdown
---
title: "Dune"
author: "Frank Herbert"
tags:
  - book
---

# Dune

**Author:** Frank Herbert

## Notes
```

Subsequent saves append entries under `## Notes`. If the file already exists (e.g. created by another plugin or manually), marginalia appends to the existing `## Notes` section without touching the rest.

---

## Note entry formats

### Manual save (AI: Save Note)

When you select text and tap **AI: Save Note**:

```markdown
- 2026-06-16 (52%):
  > The selected passage text appears here as a block quote.

  Any context you typed in the dialog appears here.
```

### Ask AI capture (auto-capture on)

When you select text, ask a question, and get an AI answer:

```markdown
- 2026-06-16 (52%) — Ask AI · Who / What is this?:
  > The selected passage text.

  Surrounding context snippet.

  **AI:** The AI’s answer appears here, formatted as prose.
  Multi-line answers are indented to stay within the list item.
```

The header `— Ask AI · <mode>` identifies the lookup type. The `**AI:**` label prefixes the answer.

### Chat Q&A (To Book Note)

When you ask a freeform question via the Ask AI chat dialog and tap **To Book Note**:

```markdown
- 2026-06-16 (52%) — Chat:

  **Asked:** What does the ice-nine symbolize?

  **AI:** Ice-nine is Vonnegut’s metaphor for the dual nature of human ingenuity…
```

No selected passage — just the question and answer, tagged with the source `Chat`.

---

## Standalone notes (Save as Note)

The **Save as Note** button (appears alongside **To Book Note** in every chat response) creates a separate, self-contained note rather than appending to the book’s file. These land in `MARGINALIA_CAPTURES_DIR` (default: `Notes/Captures/`).

Tapping **Save as Note** shows a title dialog pre-filled with a cleaned-up version of your question — edit it or accept and tap **Save**.

Example — `Notes/Captures/Emergency home birth supply list.md`:

```markdown
---
title: "Emergency home birth supply list"
date: 2026-06-16
source: "The Expectant Father"
author: "Jennifer Ash Rudick"
tags:
  - reading-capture
---

# Emergency home birth supply list

> *[[Jennifer Ash Rudick - The Expectant Father]] (52%)*

Chux pads (or newspapers), nonlatex gloves, suction bulb, cord clamp…
```

The wikilink back to the book note lets Obsidian’s graph connect your captures to the source book.

If you save a note with the same title twice, the second response is appended as a dated section under a `---` divider rather than overwriting the file.

### Customising the captures folder

```bash
export MARGINALIA_CAPTURES_DIR=Notes/Reading/Captures
# or an absolute path:
export MARGINALIA_CAPTURES_DIR=~/Documents/MyVault/Inbox
```

The path is resolved relative to `MARGINALIA_VAULT` if relative, or used as-is if absolute. Set it via `marginalia setup` (Step 2 of the wizard) or add it to your `.env` / LaunchAgent plist.

---

## File naming

Files are named `<Author> - <Title>.md` using the book metadata from KOReader (which comes from the EPUB's metadata tags). This may differ from what's in Calibre if the EPUB tags are stale.

Example:
- Author: `Frank Herbert`, Title: `Dune` → `Frank Herbert - Dune.md`

If marginalia is creating files with wrong author names, check the EPUB metadata. The plugin reports the author exactly as KOReader reads it from the file - this can sometimes include full name variations, initials, or "First Last" vs "Last, First" formatting.

---

## Offline queue

Notes are queued locally on the KOReader device first, then synced to the bridge when connected. This means:

- Notes are never lost to a spotty connection - they persist on-device (Android/Boox: `/sdcard/koreader/settings/marginalia/note_queue.json`; Kindle: `/mnt/us/koreader/settings/marginalia/note_queue.json`)
- The queue flushes automatically when you open a book (if the bridge is reachable)
- "Saved - will sync to vault when online" means the note is queued but the bridge wasn't reachable

If notes are stuck in the queue after the bridge is back up, open any book in KOReader to trigger the auto-flush.

---

## Integrating with your existing vault

The `Notes/Books/` path is a sensible default but you can change it with an env var - no source editing needed:

```bash
export MARGINALIA_BOOKS_DIR=~/Documents/YourVault/Readwise/Books
# or a path relative to your vault:
export MARGINALIA_BOOKS_DIR=~/Documents/YourVault/Reading/Notes
```

Add it to your `.env` or LaunchAgent plist alongside `MARGINALIA_VAULT`.

Restart the bridge after changing.

---

## Tips

**Link to the book note from your daily notes:** The file path is predictable - you can wikilink to it as `[[Frank Herbert - Dune]]` from anywhere in your vault.

**Frontmatter enrichment:** marginalia creates minimal frontmatter. You can enrich it with ratings, dates, tags, etc. - the note's frontmatter is yours to edit; marginalia only appends to the `## Notes` section.

**Template compatibility:** If you have an Obsidian templating plugin (Templater, Templates core plugin), marginalia won't conflict - it only writes to existing files or creates minimal new ones. You could pre-create the book note with your template before opening the book in KOReader, and marginalia will append to the `## Notes` section it finds.

**Search:** All your reading annotations, AI answers, and contexts are full-text searchable across your vault via Obsidian's built-in search or any plugin (Omnisearch, etc.).
