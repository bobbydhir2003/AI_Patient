#!/usr/bin/env bash
#
# activate-release.sh - Atomically swap /opt/ptai/current to point at an
# already-prepared release. This is the ONLY script in this directory that
# mutates /opt/ptai/current. It deliberately does NOT restart ptai.service or
# ptai-livekit-agent.service - restart decisions belong to the orchestrating
# caller (the future deploy workflow), which is expected to have already run
# the LiveKit fresh-activity-check gate before calling this script and before
# restarting anything afterward.
#
# Runs on the target EC2 instance (via SSM Run Command or SSH).
#
# Usage:
#   activate-release.sh --sha <full-40-char-git-sha> [--acknowledge-migrations] [--dry-run]
#
# --dry-run runs every validation and detection step exactly as a real
# activation would (release/venv readiness, migration-gate detection,
# current-target inspection) and prints exactly what WOULD change, but never
# writes the rollback pointer and never touches the 'current' symlink.
#
# Exit codes:
#   0 - activated (or already active - idempotent), or dry-run completed
#       showing what would happen
#   1 - bad arguments or release/venv not ready
#   3 - refused (or, in --dry-run, would be refused): new database migration
#       files detected and not acknowledged
set -Eeuo pipefail

log() { echo "[activate-release] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

BASE_DIR="${PTAI_BASE_DIR:-/opt/ptai}"
RELEASES_DIR="$BASE_DIR/releases"
VENVS_DIR="$BASE_DIR/venvs"
SHARED_DIR="$BASE_DIR/shared"
CURRENT_LINK="$BASE_DIR/current"
ROLLBACK_POINTER="$SHARED_DIR/rollback-pointer"

SHA=""
ACK_MIGRATIONS=false
DRY_RUN=false

while [ $# -gt 0 ]; do
  case "$1" in
    --sha) SHA="${2:-}"; shift 2 ;;
    --acknowledge-migrations) ACK_MIGRATIONS=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help)
      echo "Usage: $0 --sha <full-40-char-git-sha> [--acknowledge-migrations] [--dry-run]"
      exit 0
      ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[ -n "$SHA" ] || die "--sha is required"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "--sha must be a full 40-character lowercase hex git SHA, got: $SHA"

RELEASE_DIR="$RELEASES_DIR/$SHA"
[ -f "$RELEASE_DIR/.ready" ] || die "release is not ready (no .ready marker): $RELEASE_DIR -- run prepare-release.sh first"

VENV_HASH_FILE="$RELEASE_DIR/.venv-hash"
[ -f "$VENV_HASH_FILE" ] || die "release is missing .venv-hash: $RELEASE_DIR"
VENV_HASH=$(cat "$VENV_HASH_FILE")
VENV_ROOT="$VENVS_DIR/$VENV_HASH"
[ -f "$VENV_ROOT/backend/.ready" ] || die "backend venv is not ready: $VENV_ROOT/backend"
[ -f "$VENV_ROOT/livekit/.ready" ] || die "livekit venv is not ready: $VENV_ROOT/livekit"

if [ -L "$CURRENT_LINK" ]; then
  CURRENT_TARGET=$(readlink -f "$CURRENT_LINK" 2>/dev/null || readlink "$CURRENT_LINK")
  # Only trust this as a real prior release if it actually resolves under
  # RELEASES_DIR - a symlink corrupted or repointed by something outside
  # this script's own atomic-swap logic must never be treated as a valid
  # basis for a rollback pointer or a migration-diff comparison.
  case "$CURRENT_TARGET" in
    "$RELEASES_DIR"/*)
      CURRENT_TARGET_SHA=$(basename "$CURRENT_TARGET")
      ;;
    *)
      log "WARNING: 'current' resolves to $CURRENT_TARGET, which is outside $RELEASES_DIR -- treating as no valid prior release"
      CURRENT_TARGET=""
      CURRENT_TARGET_SHA=""
      ;;
  esac
  if [ "$CURRENT_TARGET_SHA" = "$SHA" ]; then
    log "current already points to $SHA -- nothing to do$([ "$DRY_RUN" = true ] && echo ' (dry-run)')"
    exit 0
  fi
else
  CURRENT_TARGET=""
  CURRENT_TARGET_SHA=""
fi

# Migration detection gate: never silently activate a release whose migration
# files differ from what's currently active. Activating code does not run
# Alembic, but we still refuse without an explicit acknowledgment so a human
# consciously decides how/when migrations get applied (never automatic).
MIGRATIONS_DIR="backend/app/database/migrations/versions"
NEW_MIGRATIONS=""
if [ -d "$RELEASE_DIR/$MIGRATIONS_DIR" ]; then
  if [ -n "$CURRENT_TARGET_SHA" ] && [ -d "$RELEASES_DIR/$CURRENT_TARGET_SHA/$MIGRATIONS_DIR" ]; then
    NEW_MIGRATIONS=$(comm -13 \
      <(find "$RELEASES_DIR/$CURRENT_TARGET_SHA/$MIGRATIONS_DIR" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | sort) \
      <(find "$RELEASE_DIR/$MIGRATIONS_DIR" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | sort) || true)
  elif [ -z "$CURRENT_TARGET_SHA" ]; then
    # No current release to compare against - cannot determine "new" vs
    # "existing" migrations, so treat any migration files as needing
    # acknowledgment on this very first activation.
    NEW_MIGRATIONS=$(find "$RELEASE_DIR/$MIGRATIONS_DIR" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' 2>/dev/null || true)
  fi
fi

if [ -n "$NEW_MIGRATIONS" ] && [ "$ACK_MIGRATIONS" != "true" ]; then
  if [ "$DRY_RUN" = true ]; then
    log "WOULD REFUSE (dry-run): new database migration file(s) detected in this release:"
  else
    log "REFUSED: new database migration file(s) detected in this release:"
  fi
  while IFS= read -r m; do [ -n "$m" ] && log "  - $m"; done <<< "$NEW_MIGRATIONS"
  log "This script never runs Alembic automatically. Review the migration(s),"
  log "apply them through the approved migration process, then re-run with"
  log "--acknowledge-migrations to proceed with activation."
  exit 3
fi
if [ -n "$NEW_MIGRATIONS" ]; then
  log "$([ "$DRY_RUN" = true ] && echo "Would proceed" || echo "Proceeding") with acknowledged new migration file(s) present (not executed by this script):"
  while IFS= read -r m; do [ -n "$m" ] && log "  - $m"; done <<< "$NEW_MIGRATIONS"
fi

if [ "$DRY_RUN" = true ]; then
  log "DRY-RUN: would activate current -> $RELEASE_DIR"
  if [ -n "$CURRENT_TARGET_SHA" ]; then
    log "DRY-RUN: would record rollback pointer: previous=$CURRENT_TARGET_SHA activated=$SHA"
  else
    log "DRY-RUN: no prior 'current' target -- this would be the first activation, no rollback pointer would be written"
  fi
  log "DRY-RUN: no filesystem changes were made."
  exit 0
fi

mkdir -p "$SHARED_DIR"
if [ -n "$CURRENT_TARGET_SHA" ]; then
  {
    echo "previous_sha=$CURRENT_TARGET_SHA"
    echo "activated_sha=$SHA"
    echo "timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$ROLLBACK_POINTER.tmp"
  mv -T "$ROLLBACK_POINTER.tmp" "$ROLLBACK_POINTER"
  log "Rollback pointer recorded: previous=$CURRENT_TARGET_SHA"
else
  log "No prior 'current' target -- this is the first activation, no rollback pointer written"
fi

# Atomic symlink swap: build the new link next to the target, then rename
# over the old one. `mv -T` on the same filesystem is an atomic rename, so
# there is no moment where 'current' points nowhere or is missing.
TMP_LINK="$BASE_DIR/current.tmp.$$"
ln -s "$RELEASE_DIR" "$TMP_LINK"
mv -T "$TMP_LINK" "$CURRENT_LINK"

log "Activated: current -> $RELEASE_DIR"
log "(No service was restarted by this script - that is the caller's responsibility.)"
