#!/usr/bin/env bash
#
# rollback-release.sh - Restore /opt/ptai/current to the exact previous
# release recorded by the last activate-release.sh run. Deliberately does NOT
# guess "the second newest directory" - it reads the rollback pointer file
# that activate-release.sh writes at the moment of activation, so the target
# is always the release that was actually live immediately before the last
# swap, never an assumption based on directory listing/mtime order.
#
# Like activate-release.sh, this does NOT restart any service - that is the
# caller's responsibility.
#
# Runs on the target EC2 instance (via SSM Run Command or SSH).
#
# Usage:
#   rollback-release.sh --confirm
#   rollback-release.sh --dry-run
#
# --confirm is required so this can never be triggered by an accidental
# argument-less invocation. --dry-run runs every validation step (pointer
# presence/shape, target release/venv readiness, current-state drift check)
# and prints exactly what WOULD change, but never mutates anything; it does
# not require --confirm.
#
# Exit codes:
#   0 - rolled back successfully, or dry-run completed showing what would
#       happen
#   1 - bad arguments, no rollback pointer, or target release/venv not ready
set -Eeuo pipefail

log() { echo "[rollback-release] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

BASE_DIR="${PTAI_BASE_DIR:-/opt/ptai}"
RELEASES_DIR="$BASE_DIR/releases"
VENVS_DIR="$BASE_DIR/venvs"
SHARED_DIR="$BASE_DIR/shared"
CURRENT_LINK="$BASE_DIR/current"
ROLLBACK_POINTER="$SHARED_DIR/rollback-pointer"

CONFIRMED=false
DRY_RUN=false
while [ $# -gt 0 ]; do
  case "$1" in
    --confirm) CONFIRMED=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) echo "Usage: $0 --confirm | --dry-run"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

if [ "$DRY_RUN" != "true" ]; then
  [ "$CONFIRMED" = "true" ] || die "refusing to run without --confirm (this changes live production 'current') or --dry-run"
fi
[ -f "$ROLLBACK_POINTER" ] || die "no rollback pointer found at $ROLLBACK_POINTER -- nothing recorded to roll back to"

# The trailing `|| true` matters: under `set -o pipefail`, a no-match grep
# makes the whole pipeline (and thus this plain assignment) exit non-zero,
# which `set -e` would treat as a reason to abort the script immediately -
# silently, without ever reaching the explicit "malformed" checks below.
PREVIOUS_SHA=$(grep -E '^previous_sha=' "$ROLLBACK_POINTER" | head -n1 | cut -d= -f2- || true)
ACTIVATED_SHA=$(grep -E '^activated_sha=' "$ROLLBACK_POINTER" | head -n1 | cut -d= -f2- || true)
[ -n "$PREVIOUS_SHA" ] || die "rollback pointer is malformed (no previous_sha): $ROLLBACK_POINTER"
[[ "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]] || die "rollback pointer previous_sha is not a valid 40-char SHA: $PREVIOUS_SHA"

RELEASE_DIR="$RELEASES_DIR/$PREVIOUS_SHA"
[ -f "$RELEASE_DIR/.ready" ] || die "rollback target release is not ready: $RELEASE_DIR"

VENV_HASH=$(cat "$RELEASE_DIR/.venv-hash" 2>/dev/null) || die "rollback target release missing .venv-hash: $RELEASE_DIR"
VENV_ROOT="$VENVS_DIR/$VENV_HASH"
[ -f "$VENV_ROOT/backend/.ready" ] || die "rollback target backend venv is not ready: $VENV_ROOT/backend"
[ -f "$VENV_ROOT/livekit/.ready" ] || die "rollback target livekit venv is not ready: $VENV_ROOT/livekit"

if [ -L "$CURRENT_LINK" ]; then
  CURRENT_TARGET=$(readlink -f "$CURRENT_LINK" 2>/dev/null || readlink "$CURRENT_LINK")
  # Same containment check as activate-release.sh: only trust this as a real
  # release if it actually resolves under RELEASES_DIR.
  case "$CURRENT_TARGET" in
    "$RELEASES_DIR"/*) CURRENT_TARGET_SHA=$(basename "$CURRENT_TARGET") ;;
    *)
      log "WARNING: 'current' resolves to $CURRENT_TARGET, which is outside $RELEASES_DIR"
      die "refusing to roll back against an unexpected current state -- investigate manually"
      ;;
  esac
  if [ "$CURRENT_TARGET_SHA" = "$PREVIOUS_SHA" ]; then
    log "current already points to $PREVIOUS_SHA -- nothing to do$([ "$DRY_RUN" = true ] && echo ' (dry-run)')"
    exit 0
  fi
  if [ -n "$ACTIVATED_SHA" ] && [ "$CURRENT_TARGET_SHA" != "$ACTIVATED_SHA" ]; then
    log "WARNING: current ($CURRENT_TARGET_SHA) does not match the pointer's recorded activated_sha ($ACTIVATED_SHA)."
    log "Something changed 'current' outside of activate-release.sh/rollback-release.sh since this pointer was written."
    die "refusing to roll back against an unexpected current state -- investigate manually"
  fi
else
  die "no 'current' symlink exists -- nothing to roll back from"
fi

if [ "$DRY_RUN" = true ]; then
  log "DRY-RUN: would roll back current -> $RELEASE_DIR (currently: $CURRENT_TARGET_SHA)"
  log "DRY-RUN: would record rollback pointer: previous=$CURRENT_TARGET_SHA activated=$PREVIOUS_SHA note=rollback"
  log "DRY-RUN: no filesystem changes were made."
  exit 0
fi

# Record the (about to be previous) state as the new rollback pointer, so a
# rollback can itself be rolled back/redone symmetrically.
{
  echo "previous_sha=$CURRENT_TARGET_SHA"
  echo "activated_sha=$PREVIOUS_SHA"
  echo "timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "note=rollback"
} > "$ROLLBACK_POINTER.tmp"
mv -T "$ROLLBACK_POINTER.tmp" "$ROLLBACK_POINTER"

TMP_LINK="$BASE_DIR/current.tmp.$$"
ln -s "$RELEASE_DIR" "$TMP_LINK"
mv -T "$TMP_LINK" "$CURRENT_LINK"

log "Rolled back: current -> $RELEASE_DIR (was $CURRENT_TARGET_SHA)"
log "(No service was restarted by this script - that is the caller's responsibility.)"
