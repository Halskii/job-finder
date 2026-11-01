# The web app, for running on a server. See deploy/README.md.
FROM python:3.13-slim

# CHROME_NO_SANDBOX: the browser that prints the PDFs cannot start its own sandbox inside a container (see tailor.html_to_pdf)
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CHROME_NO_SANDBOX=1

# chromium prints the resume PDFs; Carlito is the free font with the same letter widths as Calibri, so pages break as they do on a PC
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-crosextra-carlito fonts-liberation curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Everything a person owns lives in /app/data (a volume). The crawl's memory goes there too, through a link.
RUN useradd --create-home app \
    && mkdir -p /app/data \
    && ln -s /app/data/crawl_cache.json /app/crawl_cache.json \
    && chown -R app:app /app
USER app

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD curl -fsS http://localhost:8765/healthz || exit 1
CMD ["python", "web.py", "--host", "0.0.0.0", "--no-open"]
