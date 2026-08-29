#!/usr/bin/env bash
#
# fetch-artifact.sh - Downloads a release artifact from S3 using the EC2
# instance's own IAM role (no presigned URL, no static credentials),
# verifies its SHA-256 against an explicitly-supplied expected value,
# inspects the archive for unsafe entries BEFORE extraction, and extracts it
# only into a freshly-created directory beneath an isolated staging root.
# Never touches /opt/ptai/current or /opt/ptai/releases directly - the
# output is a verified staging path meant to be passed as prepare-release.sh
# --source-dir by the caller.
#
# Usage:
#   fetch-artifact.sh --bucket <name> --key <releases/SHA/ptai-release.tar.gz> \
#                      --expected-sha256 <64-hex-chars> --staging-root <path>
#
# Exit codes:
#   0 - download verified and safely extracted; staging path printed
#   1 - bad arguments, checksum mismatch, or an unsafe archive entry found
set -Eeuo pipefail

log() { echo "[fetch-artifact] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  die "requires bash >= 4.4, found ${BASH_VERSION}"
fi

BUCKET=""
KEY=""
EXPECTED_SHA256=""
STAGING_ROOT=""
REGION="us-east-2"

while [ $# -gt 0 ]; do
  case "$1" in
    --bucket) BUCKET="${2:-}"; shift 2 ;;
    --key) KEY="${2:-}"; shift 2 ;;
    --expected-sha256) EXPECTED_SHA256="${2:-}"; shift 2 ;;
    --staging-root) STAGING_ROOT="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --bucket <name> --key <releases/SHA/ptai-release.tar.gz> --expected-sha256 <64-hex> --staging-root <path> [--region <region>]"
      exit 0
      ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[ -n "$BUCKET" ] || die "--bucket is required"
[ -n "$KEY" ] || die "--key is required"
[ -n "$EXPECTED_SHA256" ] || die "--expected-sha256 is required"
[ -n "$STAGING_ROOT" ] || die "--staging-root is required"

# Strict input validation - never interpolate unvalidated values into
# downstream commands/paths.
[[ "$BUCKET" =~ ^[a-z0-9.-]{3,63}$ ]] || die "--bucket has an invalid format: $BUCKET"
[[ "$EXPECTED_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] || die "--expected-sha256 must be exactly 64 hex characters, got: $EXPECTED_SHA256"
if [[ "$KEY" =~ ^releases/([0-9a-f]{40})/ptai-release\.tar\.gz$ ]]; then
  TARGET_SHA="${BASH_REMATCH[1]}"
else
  die "--key must match releases/<40-char-git-sha>/ptai-release.tar.gz exactly, got: $KEY"
fi

command -v aws >/dev/null 2>&1 || die "aws CLI is required but not found on PATH"
command -v tar >/dev/null 2>&1 || die "tar is required but not found on PATH"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required but not found on PATH"

mkdir -p "$STAGING_ROOT"

WORKDIR=$(mktemp -d "$STAGING_ROOT/.fetch-work-XXXXXX")
cleanup_workdir() { rm -rf -- "$WORKDIR"; }
trap cleanup_workdir EXIT

DOWNLOAD_PATH="$WORKDIR/ptai-release.tar.gz"

log "Downloading s3://$BUCKET/$KEY (target SHA $TARGET_SHA) via instance role"
aws s3api get-object \
  --bucket "$BUCKET" \
  --key "$KEY" \
  --region "$REGION" \
  "$DOWNLOAD_PATH" >/dev/null

[ -f "$DOWNLOAD_PATH" ] || die "download reported success but file is missing: $DOWNLOAD_PATH"

ACTUAL_SHA256=$(sha256sum "$DOWNLOAD_PATH" | awk '{print $1}')
# Case-insensitive, constant-length comparison - both sides are already
# validated as exactly 64 hex chars, so a plain string compare after
# lowercasing both is an exact, safe comparison (no timing-side-channel
# concern: this is a local file integrity check, not an auth secret compare).
EXPECTED_LOWER=$(echo "$EXPECTED_SHA256" | tr '[:upper:]' '[:lower:]')
if [ "$ACTUAL_SHA256" != "$EXPECTED_LOWER" ]; then
  log "Checksum mismatch - expected $EXPECTED_LOWER, got $ACTUAL_SHA256"
  rm -f "$DOWNLOAD_PATH"
  die "downloaded artifact failed SHA-256 verification, deleted, not extracted"
fi
log "Checksum verified: $ACTUAL_SHA256"

# Inspect the archive BEFORE ever extracting anything.
PLAIN_NAMES=$(tar -tzf "$DOWNLOAD_PATH" 2>&1) || { rm -f "$DOWNLOAD_PATH"; die "failed to list archive contents - possibly corrupt"; }

while IFS= read -r name; do
  [ -n "$name" ] || continue
  case "$name" in
    /*)
      rm -f "$DOWNLOAD_PATH"
      die "archive contains an absolute path entry: $name"
      ;;
  esac
  case "$name" in
    ..|*../*|*/..)
      rm -f "$DOWNLOAD_PATH"
      die "archive contains a path-traversal entry: $name"
      ;;
  esac
done <<< "$PLAIN_NAMES"

VERBOSE_LISTING=$(tar -tvzf "$DOWNLOAD_PATH" 2>&1) || { rm -f "$DOWNLOAD_PATH"; die "failed to verbosely list archive contents"; }

BAD_ENTRIES=$(echo "$VERBOSE_LISTING" | awk 'substr($1,1,1) != "-" && substr($1,1,1) != "d"')
if [ -n "$BAD_ENTRIES" ]; then
  log "Archive contains disallowed entry types (only regular files/directories permitted):"
  while IFS= read -r l; do [ -n "$l" ] && log "  $l"; done <<< "$BAD_ENTRIES"
  rm -f "$DOWNLOAD_PATH"
  die "refusing to extract: disallowed entry type present (symlink/device/FIFO/socket)"
fi

HARDLINK_ENTRIES=$(echo "$VERBOSE_LISTING" | grep -F ' link to ' || true)
if [ -n "$HARDLINK_ENTRIES" ]; then
  log "Archive contains hard-linked entries:"
  while IFS= read -r l; do [ -n "$l" ] && log "  $l"; done <<< "$HARDLINK_ENTRIES"
  rm -f "$DOWNLOAD_PATH"
  die "refusing to extract: hard links are not permitted"
fi

log "Archive listing verified safe: only regular files and directories, no traversal/absolute paths, no hard links"

# Extract only into a freshly-created, uniquely-named directory beneath the
# staging root - never into /opt/ptai/current, /opt/ptai/releases, or
# anywhere pre-existing.
EXTRACT_DIR=$(mktemp -d "$STAGING_ROOT/release-${TARGET_SHA:0:12}-XXXXXX")
tar --no-same-owner --no-same-permissions -xzf "$DOWNLOAD_PATH" -C "$EXTRACT_DIR"

# Post-extraction secret scan - defense in depth, independent of whatever
# build-artifact.sh already excluded.
FOUND_SECRETS=$(find "$EXTRACT_DIR" -type f -name '.env*' ! -name '.env.example' 2>/dev/null || true)
if [ -n "$FOUND_SECRETS" ]; then
  log "ERROR: extracted artifact contains disallowed secret-shaped file(s):"
  while IFS= read -r f; do [ -n "$f" ] && log "  - ${f#"$EXTRACT_DIR"/}"; done <<< "$FOUND_SECRETS"
  rm -rf -- "$EXTRACT_DIR" "$DOWNLOAD_PATH"
  die "refusing to leave an extracted release containing .env-like files on disk"
fi

log "Extraction verified: no secret-shaped files present"
log "Verified staging path: $EXTRACT_DIR"
echo "staging_path=$EXTRACT_DIR"
