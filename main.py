import os
import threading
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from api.controller_port import router as pdf_router
from services import sessions

HERE = os.path.dirname(os.path.abspath(__file__))
BIND = os.environ.get("BIND", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8000))
# Set ALLOWED_HOSTS=pdf.example.com when hosted; default is local only (blocks DNS rebinding).
ALLOWED = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if h.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=sessions.reap_forever, daemon=True).start()
    yield


app = FastAPI(title="PDF Surgeon & Reflow Engine", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED)


@app.middleware("http")
async def same_origin_and_size(request: Request, call_next):
    # CSRF: a page on another site must not be able to drive someone's editor session
    origin = request.headers.get("origin")
    if request.method != "GET" and origin and origin.split("://", 1)[-1].lower() != request.headers.get("host", "").lower():
        return JSONResponse({"error": "forbidden"}, status_code=403)
    # refuse oversized bodies before reading them
    try:
        n = int(request.headers.get("content-length", 0))
    except ValueError:
        n = -1
    limit = sessions.MAX_UPLOAD if request.url.path == "/open" else 1 << 20
    if n < 0 or n > limit:
        return JSONResponse({"error": f"Too large (limit {limit >> 20} MB)."}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    # the front-end reads `error` (FastAPI's default is `detail`, which showed "undefined")
    return JSONResponse({"error": exc.detail, "detail": exc.detail}, status_code=exc.status_code)


app.include_router(pdf_router)


@app.get("/healthz")
def healthz():
    return {"sessions": sessions.count()}


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = os.path.join(HERE, "public/index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h2>index.html not found in project root.</h2>"


if __name__ == "__main__":
    # Sessions live in this process: run ONE worker (threads handle the concurrency).
    uvicorn.run("main:app", host=BIND, port=PORT, reload=os.environ.get("RELOAD", "1") == "1")
