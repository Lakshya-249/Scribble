"""Per-user sessions. Every browser gets a random cookie; every cookie owns its own
document, undo stack, font caches and temp folder, so users cannot see or overwrite each
other's work. Idle sessions are deleted (files included)."""
import os, re, time, shutil, secrets, tempfile, threading, http.cookies, collections
from typing import Optional

from services.font_service import new_cache

MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", 20))
TTL = int(os.environ.get("SESSION_TTL", 1800))          # seconds of inactivity
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", 60)) * 1024 * 1024
MAX_PAGES = int(os.environ.get("MAX_PAGES", 1000))
MAX_UNDO = int(os.environ.get("MAX_UNDO", 20))                       # undo steps kept per user
MAX_UNDO_BYTES = int(os.environ.get("MAX_UNDO_MB", 300)) * 1024 * 1024   # disk budget for undo+redo snapshots
RENDER_CACHE = int(os.environ.get("RENDER_CACHE", 24))               # rendered page images kept per user
HEAVY = threading.BoundedSemaphore(int(os.environ.get("WORKERS", max(2, os.cpu_count() or 2))))

ROOT = tempfile.mkdtemp(prefix="pdfsurgeon_")
_LOCK = threading.Lock()
_SESSIONS: dict = {}


class Session:
    def __init__(self):
        self.id = secrets.token_hex(16)
        self.dir = os.path.join(ROOT, self.id)
        os.makedirs(self.dir)
        self.doc = None
        self.pw = ""
        self.name = "document.pdf"  # original file name, used for the download
        self.rev = 0              # bumps on every change; page images are cached per revision
        self.render = collections.OrderedDict()   # (page, scale) -> png bytes, cleared on every change
        self.undo = []            # [(snapshot_path, ghost_copy)]
        self.redo = []
        self.ghost = {}           # page -> [[char, x, y], ...] hidden (clipped) old glyphs
        self.blocks = {}          # page -> paragraph blocks from the last /text call
        self.cache = new_cache()  # font caches (never shared between users)
        self.lock = threading.RLock()
        self.seen = time.time()

    def close(self):
        try:
            if self.doc:
                self.doc.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


def end(cookie_header: Optional[str]) -> bool:
    """Delete this browser's document and temp files right now (the 'delete my file' button)."""
    s, _ = get(cookie_header, create=False)
    if s is None:
        return False
    with s.lock:
        with _LOCK:
            _drop(s)
    return True


def _drop(s: Session):
    _SESSIONS.pop(s.id, None)
    s.close()


def get(cookie_header: Optional[str], create: bool):
    """-> (session | None, is_new). None when missing (create=False) or the server is full."""
    sid = None
    try:
        m = http.cookies.SimpleCookie(cookie_header or "").get("sid")
        sid = m.value if m and re.fullmatch(r"[0-9a-f]{32}", m.value) else None
    except Exception:
        pass
    with _LOCK:
        s = _SESSIONS.get(sid)
        if s:
            s.seen = time.time()
            return s, False
        if not create:
            return None, False
        if len(_SESSIONS) >= MAX_SESSIONS:       # make room: evict the stalest idle session
            for old in sorted(_SESSIONS.values(), key=lambda x: x.seen):
                if time.time() - old.seen > 60 and old.lock.acquire(blocking=False):
                    try:
                        _drop(old)
                    finally:
                        old.lock.release()
                    break
            if len(_SESSIONS) >= MAX_SESSIONS:
                return None, False
        s = Session()
        _SESSIONS[s.id] = s
        return s, True


def reap_once():
    now = time.time()
    with _LOCK:
        for s in [x for x in _SESSIONS.values() if now - x.seen > TTL]:
            if s.lock.acquire(blocking=False):          # never delete a session that is mid-request
                try:
                    _drop(s)
                finally:
                    s.lock.release()


def reap_forever(interval: int = 60):
    while True:
        time.sleep(interval)
        reap_once()


def count() -> int:
    return len(_SESSIONS)
