FROM python:3.11-slim

# Evitar prompts interativos e buffering de logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Instalar dependencias de sistema:
# - curl: healthcheck
# - libpango/libharfbuzz/libpangoft2: runtime do WeasyPrint (renderizacao de PDF)
# - fonts-liberation: fontes (equivalente a Times New Roman) para o PDF
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        fonts-liberation && \
    rm -rf /var/lib/apt/lists/*

# Instalar dependencias Python primeiro (cache de layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar codigo da aplicacao
COPY agents/ ./agents/
COPY api/ ./api/
COPY src/ ./src/
COPY models/ ./models/
COPY static/ ./static/
COPY templates/ ./templates/
COPY security/ ./security/
COPY data/ ./data/
COPY config.py .
COPY app.py .

# Criar diretorios para dados persistentes / saidas geradas em runtime
# (no repo antigo document_urls/ e generated_documents/ vinham com dados reais de
# clientes e eram copiados para a imagem; aqui so criamos as pastas vazias)
RUN mkdir -p uploads data/documents logs document_urls generated_documents

# Criar usuario nao-root para seguranca
RUN groupadd -r zellinho && useradd -r -g zellinho -d /app -s /sbin/nologin zellinho && \
    chown -R zellinho:zellinho /app

USER zellinho

EXPOSE 8000

HEALTHCHECK --start-period=180s --interval=30s --timeout=10s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "app.py"]
