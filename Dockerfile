# Phase 8H prospective alpha collector — BGX-RESEARCH PREFLIGHT only.
# Deployment-only branch derived from validated collector commit 87b83c1.
# This image intentionally cannot default to the trading runtime.

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir sgmllib3k \
    && pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin nexus

COPY --chown=nexus:nexus . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER nexus

CMD ["python", "-m", "alpha_collector.collector"]
