# ── TalentScope Python API (FastAPI + pyodbc) ──
# Build context: repo root (so .env.local is accessible at runtime via volume)

FROM python:3.11-slim AS base

# Install system deps for pyodbc (ODBC Driver 18 for SQL Server) and Node.js
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl gnupg2 unixodbc-dev gcc g++ && \
    # Microsoft ODBC Driver 18
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 && \
    # Node.js (needed by vendor/skill_match.cjs subprocess)
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    # Cleanup
    apt-get purge -y gnupg2 gcc g++ && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY python/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (includes python/db migrations + seed tooling)
COPY python/ ./
# Copy root .env.example as fallback (real .env.local mounted at runtime)
COPY .env.example /app/.env.local

# Ensure uploads dir exists
RUN mkdir -p /app/uploads

ENV PYTHONUNBUFFERED=1
ENV UPLOAD_DIR=/app/uploads

EXPOSE 8000

CMD ["sh", "entrypoint.sh"]
