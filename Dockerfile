FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BIND=0.0.0.0 \
    PORT=8000 \
    RELOAD=0

# Free look-alike fonts (Arial -> Liberation Sans, Calibri -> Carlito, Cambria -> Caladea ...) so edited
# text matches documents that use commercial fonts. PyMuPDF ships wheels: no compiler needed.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libglib2.0-0 libgomp1 fontconfig curl \
        fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation \
        fonts-noto-core fonts-dejavu-core \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . /app

# Run unprivileged
RUN useradd --system --create-home scribble && chown -R scribble /app
USER scribble

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# ONE process on purpose: each user's open document lives in this process's memory, so a second
# worker would not know the first worker's sessions. Threads serve concurrent users.
CMD ["python", "main.py"]
