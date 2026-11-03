# Scribble

**Edit the text of a PDF the way you would in a word processor. The layout survives, and the old text is actually gone.**

Most browser PDF editors do one of two things when you "edit" text: they paint a white box over the old words and put new text on top, or they re-type the whole line in a fallback font. The page might look fine at a glance, but the original text is still inside the file, the new text sits in the wrong font, and the indents, bullets and margins around it drift out of place.

Scribble is built around the opposite idea. It treats a PDF as a set of positioned glyphs, works out the *smallest* change that turns the old text into the new text, and makes only that change.

![A justified, hanging-indent contract clause before (left) and after (right) an edit that makes the paragraph longer. The bullet gap, the indent and the right margin are unchanged.](docs/img/contract-before-after.png)

*Left: the original clause. Right: after adding words that force a re-wrap. The `a)` column, the hanging indent, the bold run and the justified right margin all hold.*

---

## What is different

### 1. The old text is really removed

A "cover it with a white rectangle" edit leaves the original text in the file. Anyone can still copy it, search it, or recover it with `pdftotext`. That is a real problem for payslips, contracts and anything with a number you changed on purpose.

Scribble deletes the old glyphs from the content stream. When the PDF library's own redaction would shift the rest of the line (it closes the gap after a deleted glyph), Scribble redacts the shifted glyphs too and redraws them exactly where they were, using their own fonts.

Check it yourself after any edit:

```bash
pdftotext edited.pdf - | grep "old value"     # finds nothing
```

### 2. Only what changed is touched

Scribble diffs the old text against the new text at the glyph level. Untouched characters are never re-drawn. Table borders, underlines, shading and neighbouring cells stay exactly as they were. Characters that have to move (because a word got longer) keep their original baseline and spacing and are only shifted sideways, and only within the same run: a wide gap such as a column break or a bullet marker stops the shift.

Changed words are redrawn *whole*, not as stray letters, so copy, search and screen readers see `slow`, not `sl` + `ow`.

Link annotations and underlines move with their text. (A hyperlink does not stay behind at its old position when the words before it get longer.)

### 3. Paragraphs keep their structure

When an edit changes how a paragraph wraps, Scribble re-flows it using the paragraph's real geometry, not a guess:

- **Hanging indents and list markers.** `a)`, `(i)`, `1.`, `1.1` and bullets keep their gap and their column. The wrapper knows how wide that gap really is, so the first line does not overflow.
- **The real right margin.** Each line is wrapped and justified against the margin the paragraph actually had, measured from where that line starts.
- **Styled width.** Bold runs are measured in bold. Wrapping a line of mixed regular and bold text no longer lets it run past the margin.
- **First-line indents, 1.5x and double spacing.** Paragraph detection follows the leading the document already uses. It needs real evidence before joining widely spaced lines, so stacked form fields and payslip rows stay separate instead of being glued into one block.
- **Everything below moves correctly.** If a paragraph grows or shrinks by a line, the content under it follows by the right amount and keeps its alignment.

### 4. Formatting is inherited the way an editor does it

- A word you edit comes out in **one** style (the style its untouched letters already have), never `hun` bold + `dre` regular + `d` bold.
- **Faux bold is preserved.** Many Mac exports (Keynote, Pages, Preview) and design tools make bold by drawing each glyph filled *and* outlined, which no text API reports. Scribble reads the outline, reapplies it to new and moved glyphs, and repairs the outline width that PDF redaction silently resets. Without this, edited text comes out visibly thinner than the words around it.

![Text drawn with a faux-bold outline, before (top) and after (bottom) editing. The weight is the same across the whole line.](docs/img/weight-before-after.png)

### 5. Honest about fonts

Edited text is drawn with the PDF's own embedded font whenever it can be reused. If not, Scribble picks the closest installed face in the same family and weight, then a free metric-compatible look-alike (Arial to Liberation Sans, Calibri to Carlito, Cambria to Caladea, Courier New to Liberation Mono, and so on), so line lengths hold. When a different font had to be used for some characters, **it tells you which ones**, so nothing is silently substituted.

Put extra fonts you are licensed to use in `fonts/` or set `PDF_FONT_DIRS` and they are picked up automatically.

---

## And the rest of an editor

| | |
|---|---|
| **Edit text** | Click a paragraph, type. `Enter` applies single lines, `Ctrl+Enter` applies paragraphs. |
| **Add text** | Font, size, bold, italic, colour, multi-line. |
| **Erase** | Drag a box. Text, lines and images are removed from the file, not hidden. |
| **Highlight** | Four colours. |
| **Images and signatures** | PNG/JPEG/WebP/GIF, transparency kept, resized in the browser first. |
| **Undo and redo** | Full history, bounded by a disk budget per user. |
| **Find** | Searches the whole document, highlights every hit, `Enter` steps through them. |
| **Pages** | Thumbnails with rotate, duplicate, move and delete. |
| **View** | Zoom, fit-to-width, lazy page loading, dark mode, mobile layout. |
| **Protected PDFs** | Password prompt; the password is only used to open that file. |
| **Shortcuts** | `Ctrl+Z`, `Ctrl+Shift+Z`, `Ctrl+S`, `Ctrl+F`, `V` `T` `H` `E` `I`, `+` `-` `0`. |

No sign-up. No watermark.

---

## Privacy and security

- Each browser gets its own random session with its own document, undo history, font cache and temp folder. Users cannot see each other's files.
- Nothing is kept beyond that session. The shield button in the header **deletes your file from the server immediately**; idle sessions are removed automatically (default 30 minutes).
- Uploads are size-limited *while streaming*, so a request with no `Content-Length` cannot bypass the limit.
- Requests with an unexpected `Host` are refused (DNS rebinding), cross-site `POST`s are refused (CSRF), and every response carries a restrictive Content-Security-Policy, `X-Frame-Options: DENY` and `nosniff`.
- The container runs as an unprivileged user, with a health check.
- Edit errors never leak internals. The user gets a plain message and the document stays unchanged: every change is snapshotted first and rolled back on failure.

---

## How an edit works

1. **Extract.** Read every glyph with its font, size, colour, baseline, underline and outline, plus the page's vector lines (borders, underlines, rules).
2. **Cluster.** Group glyphs into lines and lines into paragraphs, using the document's own leading, indents, columns and list markers.
3. **Plan.** Diff old text against new text. Snap changes to whole words. Decide what must be erased, what must move, which underlines and links follow.
4. **Decide.** If the change fits inside the line, use the minimal-diff path. If a line break moved, text is centred, right-aligned or justified, or the line overflows, hand the paragraph to the re-flow engine.
5. **Erase for real.** Redact only the changed span and any glyphs the redaction would shift, then redraw those in place. Only if that is impossible does it fall back to clipping.
6. **Draw.** Place new glyphs with inherited style (font, size, colour, underline, faux-bold outline).
7. **Cascade.** Move everything below by exactly the height difference.
8. **Snapshot.** Every step is undoable.

---

## Run it

```bash
pip install -r requirements.txt
python main.py                      # http://127.0.0.1:8000  (RELOAD=0 to disable auto-reload)
python -m pytest tests -q
```

### Deploy

```bash
ALLOWED_HOSTS=scribble.example.com COOKIE_SECURE=1 SOURCE_URL=https://github.com/you/scribble \
  docker compose up -d --build
```

Before launch:

1. **Run exactly one process.** Each user's open document lives in memory in that process. The Dockerfile already does this; do not add `--workers`, and do not run several replicas without sticky routing *and* shared storage.
2. **Set `ALLOWED_HOSTS`** to your public host name(s).
3. **Serve over HTTPS** and set `COOKIE_SECURE=1`. Allow request bodies of at least `MAX_UPLOAD_MB` in your proxy (nginx: `client_max_body_size 64m;`).
4. **Rate-limit at the proxy.** Uploads and rendering are CPU work.
5. **Licence.** PyMuPDF is AGPL-3.0. A public service must offer users the corresponding source of the version it runs, or use an Artifex commercial licence. Set `SOURCE_URL` and a "Source code" link appears on the start screen.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Accepted `Host` headers, comma-separated |
| `COOKIE_SECURE` | `0` | Set `1` behind HTTPS |
| `BIND` / `PORT` | `127.0.0.1` / `8000` | Listen address (Docker sets `0.0.0.0`) |
| `MAX_SESSIONS` | `20` | Simultaneous users; extra visitors see a "busy" message |
| `SESSION_TTL` | `1800` | Idle seconds before a session is deleted |
| `MAX_UPLOAD_MB` / `MAX_PAGES` | `60` / `1000` | Limits for an opened PDF |
| `MAX_UNDO` / `MAX_UNDO_MB` | `20` / `300` | Undo steps and disk budget per user |
| `RENDER_CACHE` | `24` | Rendered page images cached per user |
| `WORKERS` | CPU count | Concurrent CPU-heavy operations |
| `PDF_FONT_DIRS` | | Extra font folders (path-separated) |
| `SOURCE_URL` | | AGPL source link shown in the UI |
| `LOG_LEVEL` | `INFO` | Python log level |

Rough sizing: about the PDF size times (1 + undo steps) of temp disk, and about twice the PDF size of RAM, per active user. On a 200-page synthetic document, opening took 0.04 s, rendering a page 0.05 s, an edit 0.4 s and a save (with font subsetting) about 2 s.

---

## Verify it yourself

```bash
python -m pytest tests -q                 # 61 tests
python tools/inspect_text.py file.pdf "some words"
```

`tools/inspect_text.py` shows, for any piece of text, its font, whether that font is embedded and reusable, and its render mode and stroke width. It is the first thing to run when an edit looks different from its surroundings.

The tests cover the behaviours above directly: old text is absent from the saved file; same-width edits leave neighbours within a fraction of a point; changed words come out whole; justified hanging-indent paragraphs keep their margin and marker gap for `a)`, `1.`, `(i)` and `1.1`; faux-bold keeps its weight; plain text never gains an outline; uploads cannot bypass the size limit; undo/redo and page operations keep per-page bookkeeping correct.

---

## Project layout

```
main.py                    app, security headers, host and CSRF guards
api/controller_port.py     endpoints: open, edit, pages, image, search, undo/redo, save
services/
  page_geometry.py         glyphs, vector lines, faux-bold outlines
  spatial_cluster.py       lines, paragraphs, indents, list markers
  diff_plan.py             minimal diff, whole-word snapping, moves, underlines, links
  diff_edit.py             executes a plan on the page
  obj_reflow.py            paragraph re-wrapping (margins, hanging indents, justification)
  compiler.py              fallback re-typesetting, list markers, drawing, cascade
  erase_service.py         true deletion, re-anchoring, outline repair
  font_service.py          embedded fonts, installed families, look-alike aliases
  style_runs.py            per-character style inheritance
  sessions.py              per-user sessions, limits, cleanup
public/index.html          the whole UI, no build step
tools/inspect_text.py      diagnose font and weight differences
tests/                     engine and API tests
```

---

## Known limits

- **Rotated text:** if text itself is rotated, the old glyphs are hidden rather than deleted (the page looks right, but `pdftotext` could still extract them). Use **Erase** on that region, which removes content outright.
- **Scans:** image-only PDFs have no text to edit. Use Erase and Add text. There is no OCR.
- **Fonts that cannot be reused:** if a PDF's embedded font has no usable character map and no look-alike is installed, new text uses a similar font and you are told which characters. Add the font to `fonts/` for an exact match.
- **Layouts not yet covered by tests:** multi-column pages and paragraphs inside table cells.
- **Sessions are in memory:** a server restart loses open documents. The editor detects this and offers to reopen the file you chose, but unsaved edits are gone.

---

## Licence

PyMuPDF (AGPL-3.0) is a core dependency, so Scribble as a network service must make its source available to its users. Choose your own licence for the Scribble code accordingly, and see the Deploy section above.
