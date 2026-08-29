#!/usr/bin/env bash
#
# cleanup-releases.sh - Prune old release directories under
# /opt/ptai/releases. Dry-run by default; deletion only happens with the
# explicit --execute flag. Never touches /opt/ptai/venvs (venvs are
# content-addressed and may be shared across releases - venv retention is a
# separate, not-yet-built concern), /opt/ptai/shared, or /opt/ptai/current
# itself.
#
# Never deletes:
#   - the release 'current' points to
#   - the release the rollback pointer's previous_sha refers to (so a
#     rollback immediately after cleanup still works)
#   - the N most recently prepared releases (by directory mtime), regardless
#     of the above
#
# Usage:
#   cleanup-releases.sh [--keep N] [--execute]
#
# Without --execute, this only prints what would be removed and exits 0.
#
# Exit codes:
#   0 - completed (dry-run report, or successful deletion)
#   1 - bad arguments or unsafe state detected
set -Eeuo pipefail

log() { echo "[cleanup-releases] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  die "requires bash >= 4.4, found ${BASH_VERSION}"
fi

BASE_DIR="${PTAI_BASE_DIR:-/opt/ptai}"
RELEASES_DIR="$BASE_DIR/releases"
SHARED_DIR="$BASE_DIR/shared"
CURRENT_LINK="$BASE_DIR/current"
ROLLBACK_POINTER="$SHARED_DIR/rollback-pointer"

KEEP=5
EXECUTE=false

while [ $# -gt 0 ]; do
  case "$1" in
    --keep) KEEP="${2:-}"; shift 2 ;;
    --execute) EXECUTE=true; shift ;;
    -h|--help) echo "Usage: $0 [--keep N] [--execute]"; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ "$KEEP" =~ ^[0-9]+$ ]] || die "--keep must be a non-negative integer, got: $KEEP"
[ "$KEEP" -ge 1 ] || die "--keep must be at least 1"
[ -d "$RELEASES_DIR" ] || die "releases dir not found: $RELEASES_DIR"

CURRENT_SHA=""
if [ -L "$CURRENT_LINK" ]; then
  CURRENT_SHA=$(basename "$(readlink "$CURRENT_LINK")")
fi

ROLLBACK_SHA=""
if [ -f "$ROLLBACK_POINTER" ]; then
  ROLLBACK_SHA=$(grep -E '^previous_sha=' "$ROLLBACK_POINTER" | head -n1 | cut -d= -f2- || true)
fi

# Sort release directories oldest-first by mtime.
mapfile -t ALL_RELEASES < <(
  find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %f\n' 2>/dev/null \
    | sort -n | awk '{print $2}'
)

TOTAL=${#ALL_RELEASES[@]}
log "Found $TOTAL release(s) under $RELEASES_DIR (keep=$KEEP, current=${CURRENT_SHA:-none}, rollback-target=${ROLLBACK_SHA:-none})"

if [ "$TOTAL" -le "$KEEP" ]; then
  log "Nothing to prune: $TOTAL release(s) at or below --keep=$KEEP"
  exit 0
fi

# Candidates for deletion: everything except the KEEP most recent.
PRUNE_COUNT=$((TOTAL - KEEP))
declare -a CANDIDATES=("${ALL_RELEASES[@]:0:$PRUNE_COUNT}")

declare -a TO_DELETE=()
declare -a PROTECTED=()

for sha in "${CANDIDATES[@]}"; do
  if [ "$sha" = "$CURRENT_SHA" ]; then
    PROTECTED+=("$sha (currently active)")
    continue
  fi
  if [ "$sha" = "$ROLLBACK_SHA" ]; then
    PROTECTED+=("$sha (rollback target)")
    continue
  fi
  TO_DELETE+=("$sha")
done

if [ "${#PROTECTED[@]}" -gt 0 ]; then
  log "Protected (would otherwise be pruned, but kept):"
  for p in "${PROTECTED[@]}"; do log "  - $p"; done
fi

if [ "${#TO_DELETE[@]}" -eq 0 ]; then
  log "No release directories are safe to prune after protections"
  exit 0
fi

log "Release(s) selected for pruning ($([ "$EXECUTE" = true ] && echo EXECUTE || echo DRY-RUN)):"
for sha in "${TO_DELETE[@]}"; do log "  - $sha"; done

if [ "$EXECUTE" != "true" ]; then
  log "Dry-run only - no changes made. Re-run with --execute to actually delete."
  exit 0
fi

for sha in "${TO_DELETE[@]}"; do
  target="$RELEASES_DIR/$sha"
  # Final belt-and-suspenders check immediately before deletion.
  [ "$sha" != "$CURRENT_SHA" ] || die "refusing to delete currently-active release: $sha"
  [ "$sha" != "$ROLLBACK_SHA" ] || die "refusing to delete rollback-target release: $sha"
  rm -rf -- "$target"
  log "Deleted: $target"
done

log "Cleanup complete: removed ${#TO_DELETE[@]} release(s), kept $((TOTAL - ${#TO_DELETE[@]}))"
