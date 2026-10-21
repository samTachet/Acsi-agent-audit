FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /app

# Dépendances d'abord (couche mise en cache tant que pyproject/uv.lock ne changent pas)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY specs ./specs
COPY scenarios ./scenarios
COPY data/fixtures.json ./data/fixtures.json
RUN uv sync --frozen --no-dev

# Utilisateur sans privilèges ; seul /app/data/journal est inscriptible
RUN useradd --system --uid 10001 --no-create-home acsi \
 && mkdir -p /app/data/journal && chown -R acsi /app/data/journal
USER acsi
ENV PATH="/app/.venv/bin:$PATH" ACSI_ROOT=/app ACSI_JOURNAL_DIR=/app/data/journal ACSI_BACKEND=mock

# Par défaut : évaluation des scénarios de référence (mocks, sans réseau)
CMD ["python", "-m", "acsi_agent.eval"]
