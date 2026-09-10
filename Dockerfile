# ── BGX Capital Trading Bot — hardened production image ───────────
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# setuptools/wheel first: sgmllib3k is distributed as sdist.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir sgmllib3k \
    && pip install --no-cache-dir -r requirements.txt

# Run the trading service as an unprivileged account.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin nexus

COPY --chown=nexus:nexus . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=info \
    PORT=8000

USER nexus

EXPOSE 8000

# Railway owns liveness/readiness probing; keep one worker because runtime
# coordination is intentionally in-process. main_hardened adds /ready and
# destructive-admin protection without changing trading logic.
CMD ["sh", "-c", "uvicorn main_hardened:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --log-level $(echo ${LOG_LEVEL:-info} | tr '[:upper:]' '[:lower:]')"]
