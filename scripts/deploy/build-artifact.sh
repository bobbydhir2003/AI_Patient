#!/usr/bin/env bash
#
# build-artifact.sh - Builds a deterministic release artifact tarball for an
# exact git SHA from an already-checked-out repository. Runs on the GitHub
# Actions runner (or any machine with a full checkout) - never on production
# EC2. The output is what fetch-artifact.sh downloads and prepare-release.sh
# consumes as --source-dir once extracted.
#
# This script does NOT build the frontend itself (whether that's needed
# depends on classify-changes.sh output, decided by the caller/workflow) -
# it requires dist/ to already exist at --repo-path.
#
# Usage:
#   build-artifact.sh --sha <full-40-char-git-sha> --repo-path <path> --output <path-to.tar.gz>
#
# Exit codes:
#   0 - artifact built successfully, path + SHA-256 printed
#   1 - bad arguments, verification failure, or a prohibited file was found
set -Eeuo pipefail

log() { echo "[build-artifact] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  die "requires bash >= 4.4, found ${BASH_VERSION}"
fi

SHA=""
REPO_PATH=""
OUTPUT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --sha) SHA="${2:-}"; shift 2 ;;
    --repo-path) REPO_PATH="${2:-}"; shift 2 ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --sha <full-40-char-git-sha> --repo-path <path> --output <path-to.tar.gz>"
      exit 0
      ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[ -n "$SHA" ] || die "--sha is required"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "--sha must be a full 40-character lowercase hex git SHA, got: $SHA"
[ -n "$REPO_PATH" ] || die "--repo-path is required"
[ -d "$REPO_PATH" ] || die "repo path not found: $REPO_PATH"
[ -n "$OUTPUT" ] || die "--output is required"

command -v git >/dev/null 2>&1 || die "git is required but not found on PATH"
command -v tar >/dev/null 2>&1 || die "tar is required but not found on PATH"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required but not found on PATH"

# Verify the checkout is genuinely at the exact target SHA - never build an
# artifact claiming to be a SHA it doesn't actually match.
ACTUAL_SHA=$(git -C "$REPO_PATH" rev-parse HEAD 2>/dev/null) || die "repo-path is not a git repository: $REPO_PATH"
[ "$ACTUAL_SHA" = "$SHA" ] || die "checkout at $REPO_PATH is at $ACTUAL_SHA, not the requested $SHA"

[ -f "$REPO_PATH/backend/requirements.lock.txt" ] || die "backend/requirements.lock.txt not found - this is the authoritative install artifact and must exist before building a release artifact"
[ -d "$REPO_PATH/dist" ] || die "dist/ not found at $REPO_PATH - frontend must already be built by the caller before running this script"
[ -d "$REPO_PATH/scripts/deploy" ] || die "scripts/deploy/ not found at $REPO_PATH"

STAGING=$(mktemp -d)
trap 'rm -rf -- "$STAGING"' EXIT

log "Staging backend/, dist/, scripts/deploy/ for $SHA"

# rsync-equivalent copy with explicit exclusions. Using cp + find-based
# pruning (not assuming rsync is present on every possible future runner)
# would be more verbose; rsync is already a hard dependency of
# prepare-release.sh and is present on GitHub-hosted ubuntu-latest runners.
command -v rsync >/dev/null 2>&1 || die "rsync is required but not found on PATH"

copy_excluding_secrets() {
  local src="$1" dst="$2"
  rsync -a \
    --include '.env.example' \
    --exclude '.env' \
    --exclude '.env.*' \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude 'node_modules' \
    --exclude '.venv' \
    --exclude 'venv' \
    --exclude '.DS_Store' \
    --exclude '.vscode' \
    --exclude '.idea' \
    --exclude '*.swp' \
    --exclude '*.tmp' \
    --exclude '.pytest_cache' \
    --exclude '.mypy_cache' \
    --exclude '.ruff_cache' \
    "$src" "$dst"
}

copy_excluding_secrets "$REPO_PATH/backend" "$STAGING/"
copy_excluding_secrets "$REPO_PATH/dist" "$STAGING/"
copy_excluding_secrets "$REPO_PATH/scripts" "$STAGING/"

# Post-copy secret scan - defense in depth, independent of whether the
# exclude rules above worked as intended.
FOUND_SECRETS=$(find "$STAGING" -type f -name '.env*' ! -name '.env.example' 2>/dev/null || true)
if [ -n "$FOUND_SECRETS" ]; then
  log "ERROR: staged artifact contains disallowed secret-shaped file(s):"
  while IFS= read -r f; do [ -n "$f" ] && log "  - ${f#"$STAGING"/}"; done <<< "$FOUND_SECRETS"
  die "refusing to build an artifact containing .env-like files"
fi

# Reject anything that isn't a plain file or directory. This artifact type
# (source + built frontend + deploy scripts) never legitimately needs
# symlinks, hardlinks, device files, or FIFOs.
SPECIAL_ENTRIES=$(find "$STAGING" \( -type l -o -type c -o -type b -o -type p -o -type s \) 2>/dev/null || true)
if [ -n "$SPECIAL_ENTRIES" ]; then
  log "ERROR: staged artifact contains disallowed special filesystem entries:"
  while IFS= read -r f; do [ -n "$f" ] && log "  - ${f#"$STAGING"/}"; done <<< "$SPECIAL_ENTRIES"
  die "refusing to build an artifact containing symlinks/device files/FIFOs/sockets"
fi
HARDLINKED=$(find "$STAGING" -type f -links +1 2>/dev/null || true)
if [ -n "$HARDLINKED" ]; then
  log "ERROR: staged artifact contains hard-linked file(s):"
  while IFS= read -r f; do [ -n "$f" ] && log "  - ${f#"$STAGING"/}"; done <<< "$HARDLINKED"
  die "refusing to build an artifact containing hard links"
fi

[ -f "$STAGING/backend/requirements.lock.txt" ] || die "internal error: requirements.lock.txt missing from staged output"

mkdir -p "$(dirname "$OUTPUT")"

# Deterministic archive: fixed mtimes/ownership, name-sorted entries. Two
# builds of the identical staged tree produce a byte-identical tarball.
tar \
  --sort=name \
  --mtime='UTC 2020-01-01' \
  --owner=0 --group=0 --numeric-owner \
  -czf "$OUTPUT" \
  -C "$STAGING" .

SHA256=$(sha256sum "$OUTPUT" | awk '{print $1}')

log "Artifact built: $OUTPUT"
log "SHA-256: $SHA256"
echo "artifact_path=$OUTPUT"
echo "artifact_sha256=$SHA256"
