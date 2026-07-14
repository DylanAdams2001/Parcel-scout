FROM python:3.11-slim

# scraper.py launches Chromium headed (headless=False), not headless - it's
# part of evading realestate.com.au's Kasada bot detection. A headed browser
# still needs *some* display server to render into, so Xvfb provides a
# virtual one even though nothing is ever actually shown on screen here.
RUN apt-get update && apt-get install -y --no-install-recommends xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
RUN python -m patchright install --with-deps chromium

COPY backend/ backend/
COPY frontend/ frontend/

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# Render (and similar hosts) assign the port via $PORT at runtime; 8000 is
# just the local-Docker-testing default.
CMD ["sh", "-c", "xvfb-run -a uvicorn main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000}"]
