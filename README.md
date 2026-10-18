# PDF Surgeon & Reflow Engine

    uv sync            # or: pip install fastapi uvicorn pymupdf numpy
    python main.py     # http://localhost:8000

## Hosting / many users
Every browser gets its own session (cookie): own document, undo stack, font caches and temp
folder. Idle sessions are deleted. Sessions live in process memory, so run **one** uvicorn
worker (threads serve the requests): `RELOAD=0 BIND=127.0.0.1 ALLOWED_HOSTS=pdf.example.com python main.py`
behind a reverse proxy that terminates HTTPS.

| env | default | meaning |
|---|---|---|
| `BIND`, `PORT` | 127.0.0.1, 8000 | listen address |
| `ALLOWED_HOSTS` | localhost,127.0.0.1,testserver | accepted `Host` headers (set to your domain) |
| `MAX_UPLOAD_MB` | 60 | largest PDF |
| `MAX_PAGES` | 1000 | most pages per PDF |
| `MAX_SESSIONS` | 20 | simultaneous users (extra users get "busy") |
| `SESSION_TTL` | 1800 | idle seconds before a session and its files are deleted |
| `WORKERS` | CPU count | parallel heavy jobs (render / edit / save) |
| `RELOAD` | 1 | auto-reload for development; use 0 in production |

`/healthz` shows the number of live sessions.
MuPDF/PyMuPDF is AGPL: a public service must offer its source to users (or buy a commercial licence).

## Tests
    pip install httpx pytest
    python -m pytest tests/test_services.py -q

## Editing fidelity (v2)
* Every character keeps its own font / size / colour (`services/style_runs.py`); editing `30,000` → `35,000`
  in a bold amount stays bold, and the `₹` is untouched.
* Fonts are matched by family + weight (`Ubuntu-Regular` finds `Ubuntu-R.ttf`). Put the fonts your PDFs use in `./fonts`
  or set `PDF_FONT_DIRS`. Subset fonts embedded by browsers/payroll tools cannot draw new letters, so the
  installed font of the same family is what makes edits look identical.
* One-line cells are never re-wrapped; numbers in a right-aligned column keep their right edge.
* The status bar lists characters that had to come from a different font.
* `python tools/diagnose.py file.pdf "30,000.00" "35,000.00"` explains any bad edit.

## v2.1
* Per-user sessions are wired in (`api/controller_port.py`); the old single-user controller is kept as
  `api/controller_port_single_user.bak`. Behind HTTPS set `COOKIE_SECURE=1`.
* Font look-alikes: Arial/Helvetica → Liberation Sans, Calibri → Carlito, Cambria → Caladea, Times → Liberation Serif ...
  (`ALIASES` in `services/font_service.py`; the free fonts must be in `./fonts` or installed).
* Missing bold weight is imitated (fill + thin stroke) instead of silently turning regular.
* "Add text" now really uses serif/mono (it used to fall back to sans) and handles ₹ and other symbols.
* Run the full suite once: `python -m pytest tests/test_services.py tests/test_fonts.py tests/test_style_runs.py -q`
