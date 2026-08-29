# ── BGX Capital Trading Bot — Dockerfile v12.2 ───────────────────
# CORREÇÕES PARA O DEPLOY TRAVADO:
#
# 1. sgmllib3k (dependência do feedparser) NÃO tem wheel publicado —
#    só existe como sdist e precisa de setuptools para instalar.
#    A imagem slim sem build-essential fazia o pip ficar preso nessa
#    etapa, travando o build indefinidamente.
#    Fix: instala sgmllib3k explicitamente antes, com --no-build-isolation.
#
# 2. HEALTHCHECK do Docker removido — era redundante com o
#    healthcheckPath do railway.toml. Ter os dois pode manter o
#    container em estado "starting" mesmo com o app respondendo.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# setuptools/wheel primeiro: necessários para o único pacote sem wheel
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir sgmllib3k \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=info \
    PORT=8000

EXPOSE 8000

# Sem HEALTHCHECK — o Railway monitora via railway.toml (/health)

# Workers=1 obrigatório — estado compartilhado em memória
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --log-level ${LOG_LEVEL:-info}"]
