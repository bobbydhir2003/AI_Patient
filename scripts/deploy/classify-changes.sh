#!/usr/bin/env bash
#
# classify-changes.sh - Classify which files changed between two git refs and
# derive the deployment actions those changes require. Pure computation, no
# filesystem mutation, no network access, no production contact. Intended to
# run on the GitHub Actions runner (needs full git history for both refs).
#
# Usage:
#   classify-changes.sh --base <sha-or-ref> --target <sha-or-ref> [--repo-path <path>]
#
# Output: a single JSON object on stdout (see README section below for shape).
# Exit codes:
#   0 - classification succeeded (regardless of what was found)
#   1 - bad arguments, git failure, or invalid refs
set -Eeuo pipefail

log() { echo "[classify-changes] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# Requires bash >= 4.4: "${arr[@]}" on a declared-but-empty array only
# reliably expands to zero words (rather than one empty-string word) under
# `set -u` from 4.4 onward. macOS ships bash 3.2 by default - this script is
# meant to run on the Ubuntu EC2 target or a modern local bash.
if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  die "requires bash >= 4.4, found ${BASH_VERSION}"
fi

BASE_REF=""
TARGET_REF=""
REPO_PATH="."

while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE_REF="${2:-}"; shift 2 ;;
    --target) TARGET_REF="${2:-}"; shift 2 ;;
    --repo-path) REPO_PATH="${2:-}"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --base <sha-or-ref> --target <sha-or-ref> [--repo-path <path>]"
      exit 0
      ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[ -n "$BASE_REF" ] || die "--base is required"
[ -n "$TARGET_REF" ] || die "--target is required"
command -v jq >/dev/null 2>&1 || die "jq is required but not found on PATH"
command -v git >/dev/null 2>&1 || die "git is required but not found on PATH"

cd "$REPO_PATH" || die "repo path not found: $REPO_PATH"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git repository: $REPO_PATH"

BASE_SHA=$(git rev-parse --verify "${BASE_REF}^{commit}" 2>/dev/null) || die "invalid --base ref: $BASE_REF"
TARGET_SHA=$(git rev-parse --verify "${TARGET_REF}^{commit}" 2>/dev/null) || die "invalid --target ref: $TARGET_REF"

log "Diffing $BASE_SHA..$TARGET_SHA"

mapfile -t CHANGED_FILES < <(git diff --name-only "$BASE_SHA" "$TARGET_SHA" -- || true)

# Category buckets, populated by path pattern below. Order matters: first
# matching pattern wins, most-specific first.
declare -a CAT_FRONTEND=()
declare -a CAT_LIVEKIT=()
declare -a CAT_FASTAPI=()
declare -a CAT_DEPENDENCIES=()
declare -a CAT_DATABASE_MIGRATION=()
declare -a CAT_NGINX=()
declare -a CAT_SHARED_BACKEND=()
declare -a CAT_NONE=()
declare -a CAT_UNKNOWN=()

classify_one() {
  local f="$1"
  case "$f" in
    backend/requirements.txt|backend/requirements.lock.txt)
      CAT_DEPENDENCIES+=("$f") ;;
    backend/.env.example)
      # Documents available environment variables for operators; never read
      # by the running process. Production secrets are injected entirely
      # via /opt/ptai/shared/backend.env (systemd EnvironmentFile= for both
      # ptai.service and ptai-livekit-agent.service - see prepare-release.sh
      # and docs/production-deployment-architecture.md), never from any file
      # inside the release checkout itself. A template/reference file, not
      # application code - no restart or venv rebuild follows from it.
      CAT_NONE+=("$f") ;;
    backend/app/database/migrations/versions/*)
      CAT_DATABASE_MIGRATION+=("$f") ;;
    backend/app/livekit_agent/*)
      CAT_LIVEKIT+=("$f") ;;
    backend/app/api/*|backend/app/schemas/*|backend/app/main.py)
      CAT_FASTAPI+=("$f") ;;
    deploy/nginx/*|nginx/*|*.nginx.conf)
      CAT_NGINX+=("$f") ;;
    src/*|public/*|index.html|vite.config.ts|vite.config.js|tsconfig*.json|package.json|package-lock.json)
      CAT_FRONTEND+=("$f") ;;
    backend/app/*)
      CAT_SHARED_BACKEND+=("$f") ;;
    backend/tests/*|docs/*|*.md|README*|.github/*|scripts/test-*|scripts/deploy/*|scripts/production-preflight.sh)
      # Deployment tooling itself (this classifier, the release
      # prepare/activate/rollback scripts, this workflow, its docs/tests) is
      # never part of the running ptai/livekit-agent process and is invoked
      # only as one-off tooling during a deployment - a change here requires
      # no restart/venv-rebuild of the live service. Genuinely unrecognized
      # paths still fall through to the UNKNOWN case below unchanged.
      CAT_NONE+=("$f") ;;
    *)
      CAT_UNKNOWN+=("$f") ;;
  esac
}

for f in "${CHANGED_FILES[@]}"; do
  [ -n "$f" ] || continue
  classify_one "$f"
done

json_array() {
  # Emits a JSON array from the given bash array args, safely quoted.
  local arr=("$@")
  if [ "${#arr[@]}" -eq 0 ]; then
    echo "[]"
    return
  fi
  printf '%s\n' "${arr[@]}" | jq -R . | jq -s .
}

FRONTEND_JSON=$(json_array "${CAT_FRONTEND[@]}")
LIVEKIT_JSON=$(json_array "${CAT_LIVEKIT[@]}")
FASTAPI_JSON=$(json_array "${CAT_FASTAPI[@]}")
DEPENDENCIES_JSON=$(json_array "${CAT_DEPENDENCIES[@]}")
MIGRATION_JSON=$(json_array "${CAT_DATABASE_MIGRATION[@]}")
NGINX_JSON=$(json_array "${CAT_NGINX[@]}")
SHARED_BACKEND_JSON=$(json_array "${CAT_SHARED_BACKEND[@]}")
NONE_JSON=$(json_array "${CAT_NONE[@]}")
UNKNOWN_JSON=$(json_array "${CAT_UNKNOWN[@]}")
CHANGED_FILES_JSON=$(json_array "${CHANGED_FILES[@]}")

nonempty() { [ "$1" != "[]" ]; }

BUILD_FRONTEND=false; nonempty "$FRONTEND_JSON" && BUILD_FRONTEND=true

RESTART_PTAI=false
if nonempty "$FASTAPI_JSON" || nonempty "$SHARED_BACKEND_JSON" || nonempty "$DEPENDENCIES_JSON" \
   || nonempty "$MIGRATION_JSON" || nonempty "$UNKNOWN_JSON"; then
  RESTART_PTAI=true
fi

RESTART_LIVEKIT=false
if nonempty "$LIVEKIT_JSON" || nonempty "$SHARED_BACKEND_JSON" || nonempty "$DEPENDENCIES_JSON" \
   || nonempty "$MIGRATION_JSON" || nonempty "$UNKNOWN_JSON"; then
  RESTART_LIVEKIT=true
fi

VENV_REBUILD=false
if nonempty "$DEPENDENCIES_JSON" || nonempty "$UNKNOWN_JSON"; then
  VENV_REBUILD=true
fi

MIGRATION_GATE=false; nonempty "$MIGRATION_JSON" && MIGRATION_GATE=true
NGINX_GATE=false; nonempty "$NGINX_JSON" && NGINX_GATE=true

jq -n \
  --arg base_sha "$BASE_SHA" \
  --arg target_sha "$TARGET_SHA" \
  --argjson changed_files "$CHANGED_FILES_JSON" \
  --argjson cat_frontend "$FRONTEND_JSON" \
  --argjson cat_livekit "$LIVEKIT_JSON" \
  --argjson cat_fastapi "$FASTAPI_JSON" \
  --argjson cat_dependencies "$DEPENDENCIES_JSON" \
  --argjson cat_migration "$MIGRATION_JSON" \
  --argjson cat_nginx "$NGINX_JSON" \
  --argjson cat_shared_backend "$SHARED_BACKEND_JSON" \
  --argjson cat_none "$NONE_JSON" \
  --argjson cat_unknown "$UNKNOWN_JSON" \
  --argjson build_frontend "$BUILD_FRONTEND" \
  --argjson restart_ptai "$RESTART_PTAI" \
  --argjson restart_livekit "$RESTART_LIVEKIT" \
  --argjson venv_rebuild "$VENV_REBUILD" \
  --argjson migration_gate "$MIGRATION_GATE" \
  --argjson nginx_gate "$NGINX_GATE" \
  '{
    base_sha: $base_sha,
    target_sha: $target_sha,
    changed_files: $changed_files,
    categories: {
      FRONTEND: $cat_frontend,
      LIVEKIT: $cat_livekit,
      FASTAPI: $cat_fastapi,
      DEPENDENCIES: $cat_dependencies,
      DATABASE_MIGRATION: $cat_migration,
      NGINX: $cat_nginx,
      SHARED_BACKEND: $cat_shared_backend,
      NONE: $cat_none,
      UNKNOWN: $cat_unknown
    },
    actions: {
      build_frontend: $build_frontend,
      restart_ptai: $restart_ptai,
      restart_livekit: $restart_livekit,
      venv_rebuild: $venv_rebuild,
      migration_gate: $migration_gate,
      nginx_gate: $nginx_gate
    }
  }'

log "Classification complete: ${#CHANGED_FILES[@]} file(s) changed"
