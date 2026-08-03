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

# A11: create and run as a non-root user. Files are owned by the app user so the
# process has no unnecessary write access to the image contents.
RUN adduser --disabled-password --gecos "" --uid 10001 appuser \
    && chown -R appuser:appuser /srv/backend
USER appuser

EXPOSE 8000

# Deployment command is unchanged (preserves AWS compatibility).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
