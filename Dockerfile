# ── BGX Capital Trading Bot — Dockerfile v11.0 ───────────────────
# INFRA-2: Atualizado para incluir optuna e dependências de compilação
FROM python:3.11-slim

# Dependências de sistema:
#   curl  — healthcheck
#   gcc, g++, make — necessário para compilar optuna e algumas deps numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gcc g++ make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependências primeiro (cache de layer otimizado)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copia o código
COPY . .

# Variáveis de ambiente padrão (sobrescritas no Railway)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO \
    PORT=8000

# Diretório para SQLite e params_optimized.json
# Em Railway, monte um volume em /data para persistência entre deploys
RUN mkdir -p /data && chmod 777 /data

# Healthcheck: verifica se o servidor está respondendo
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

# Start com workers=1 (estado compartilhado em memória — não usar múltiplos workers)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --log-level ${LOG_LEVEL:-info}"]
