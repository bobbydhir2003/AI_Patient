#!/usr/bin/env sh
# Container entrypoint: apply pending database migrations, THEN start the API.
#
# `alembic upgrade head` is idempotent and additive — it only runs migrations
# that have not yet been applied (e.g. 0017 ai_usage_events on an environment
# still at 0016) and never destroys existing data. Running it here guarantees
# the deployed schema matches the code, so endpoints can never 500 with
# "no such table". Alembic (not create_all) remains the source of truth.
#
# RUN_MIGRATIONS=false lets an operator skip this step if migrations are handled
# by a separate pipeline stage (e.g. an ECS/CodeDeploy migration task).
set -e

if [ "${RUN_MIGRATIONS:-true}" != "false" ]; then
  echo "[start] Applying database migrations (alembic upgrade head)…"
  alembic upgrade head
else
  echo "[start] RUN_MIGRATIONS=false — skipping migrations."
fi

echo "[start] Starting API…"
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
