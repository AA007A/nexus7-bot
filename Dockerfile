# ── BGX Capital Trading Bot — Dockerfile v11.1 ───────────────────
# Otimizado para Railway Hobby Plan:
#   - Sem gcc/g++/make — todas as deps têm wheels pré-compilados
#   - python:3.11-slim base mínima
#   - Build cache otimizado (requirements antes do código)
FROM python:3.11-slim

# Apenas curl para o healthcheck — sem compiladores desnecessários
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependências (camada cacheada — só rebuilda se requirements.txt mudar)
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

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

# Workers=1 obrigatório — estado compartilhado em memória
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --log-level ${LOG_LEVEL:-info}"]
