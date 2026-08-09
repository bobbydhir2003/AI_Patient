FROM python:3.12-slim

# Don't buffer stdout/stderr (clean container logs) and don't write .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/backend

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application. Secrets, .git, venvs, local DBs, caches and tests are
# excluded by .dockerignore, so they are never baked into the image.
COPY . .

# Make the entrypoint executable before dropping privileges.
RUN chmod +x scripts/start.sh

# A11: create and run as a non-root user. Files are owned by the app user so the
# process has no unnecessary write access to the image contents.
RUN adduser --disabled-password --gecos "" --uid 10001 appuser \
    && chown -R appuser:appuser /srv/backend
USER appuser

EXPOSE 8000

# Apply pending DB migrations (idempotent, additive) and THEN start the API, so
# the deployed schema always matches the code and endpoints can't 500 on a
# missing table. Set RUN_MIGRATIONS=false to skip when a separate migration
# pipeline stage handles it.
CMD ["sh", "scripts/start.sh"]
