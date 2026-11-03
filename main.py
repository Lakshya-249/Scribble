import logging
import os
import threading
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from api.controller_port import router as pdf_router
from services import sessions

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scribble")

HERE = os.path.dirname(os.path.abspath(__file__))
BIND = os.environ.get("BIND", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8000))
# Set ALLOWED_HOSTS=scribble.example.com when hosted; default is local only (blocks DNS rebinding).
ALLOWED = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,scrible.slingshot.quest,www.scrible.slingshot.quest").split(",") if h.strip()]

# The page ships its own inline CSS/JS and talks only to this origin.
# CSP = ("default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; "
#        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

# The page ships its own inline CSS/JS and talks only to this origin.
CSP = (
    "default-src 'self'; "
    "img-src 'self' blob: data:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=sessions.reap_forever, daemon=True).start()
    yield


app = FastAPI(title="Scribble", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED)


@app.middleware("http")
async def guard(request: Request, call_next):
    # CSRF: a page on another site must not be able to drive someone's editor session
    origin = request.headers.get("origin")
    if request.method != "GET" and origin and origin.split("://", 1)[-1].lower() != request.headers.get("host", "").lower():
        return JSONResponse({"error": "forbidden"}, status_code=403)
    # Cheap early refusal when the client declares its size. /open also enforces the limit while
    # streaming, because a chunked upload has no Content-Length at all.
    limit = sessions.MAX_UPLOAD if request.url.path in ("/open", "/image") else 1 << 20
    try:
        n = int(request.headers.get("content-length", 0))
    except ValueError:
        n = -1
    if n < 0 or n > limit:
        return JSONResponse({"error": f"Too large (limit {limit >> 20} MB)."}, status_code=413)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", CSP)
    if request.url.path != "/" or request.method != "GET":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    # the front-end reads `error` (FastAPI's default is `detail`, which showed "undefined")
    return JSONResponse({"error": exc.detail, "detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("unhandled error")
    msg = "Something went wrong on our side. Your document is safe: try again."
    return JSONResponse({"error": msg, "detail": msg}, status_code=500)


app.include_router(pdf_router)


@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    return {"ok": True, "sessions": sessions.count()}


@app.get("/info")
def info():
    return {"max_upload_mb": sessions.MAX_UPLOAD >> 20, "ttl_min": sessions.TTL // 60,
            "source_url": os.environ.get("SOURCE_URL", "")}


INDEX = os.path.join(HERE, "public", "index.html")


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index():
    with open(INDEX, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    # Sessions live in this process: run ONE worker (threads handle the concurrency).
    uvicorn.run("main:app", host=BIND, port=PORT, reload=os.environ.get("RELOAD", "1") == "1")
