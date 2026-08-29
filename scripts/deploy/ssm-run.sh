#!/usr/bin/env bash
#
# ssm-run.sh - Send a shell command to an EC2 instance via AWS SSM Run
# Command and wait for completion with a bounded, finite timeout, printing
# structured results (status, stdout, stderr). Used by
# deploy-production.yml to keep each workflow step small and focused rather
# than repeating send/poll/retrieve boilerplate inline six times.
#
# Requires AWS credentials already configured in the environment (e.g. via
# GitHub OIDC in the calling workflow) - this script never handles
# credentials itself.
#
# Usage:
#   ssm-run.sh --instance-id <id> --region <region> \
#              (--command-file <path> | --command <literal>) \
#              [--timeout-seconds <n>] [--poll-interval-seconds <n>]
#
# Output (stdout): one line per field, always in this order regardless of
# outcome, so callers can reliably parse with `grep '^key=' | cut -d= -f2-`:
#   command_id=<id>
#   status=<Success|Failed|TimedOut|Cancelled|...|POLL_TIMEOUT>
#   exit_status=<N>
# Then a delimiter line `---STDOUT---`, then the command's real stdout
# verbatim, then `---STDERR---`, then the command's real stderr verbatim.
#
# Exit codes:
#   0 - command completed with Status=Success
#   1 - bad arguments
#   2 - polling exceeded --timeout-seconds without reaching a terminal state
#   3 - command reached a terminal state but Status != Success
set -Eeuo pipefail

log() { echo "[ssm-run] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

INSTANCE_ID=""
REGION=""
COMMAND_FILE=""
COMMAND_LITERAL=""
TIMEOUT_SECONDS=300
POLL_INTERVAL_SECONDS=5

while [ $# -gt 0 ]; do
  case "$1" in
    --instance-id) INSTANCE_ID="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --command-file) COMMAND_FILE="${2:-}"; shift 2 ;;
    --command) COMMAND_LITERAL="${2:-}"; shift 2 ;;
    --timeout-seconds) TIMEOUT_SECONDS="${2:-}"; shift 2 ;;
    --poll-interval-seconds) POLL_INTERVAL_SECONDS="${2:-}"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 --instance-id <id> --region <region> (--command-file <path> | --command <literal>) [--timeout-seconds <n>] [--poll-interval-seconds <n>]"
      exit 0
      ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[ -n "$INSTANCE_ID" ] || die "--instance-id is required"
[ -n "$REGION" ] || die "--region is required"
[[ "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || die "--timeout-seconds must be a positive integer"
[[ "$POLL_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || die "--poll-interval-seconds must be a positive integer"

if [ -n "$COMMAND_FILE" ]; then
  [ -f "$COMMAND_FILE" ] || die "--command-file not found: $COMMAND_FILE"
elif [ -n "$COMMAND_LITERAL" ]; then
  : # handled below by writing into a temp file alongside the --command-file case
else
  die "one of --command-file or --command is required"
fi

command -v aws >/dev/null 2>&1 || die "aws CLI is required but not found on PATH"
command -v jq >/dev/null 2>&1 || die "jq is required but not found on PATH"

PARAMS_FILE=$(mktemp)
COMMAND_TEXT_FILE=$(mktemp)
trap 'rm -f "$PARAMS_FILE" "$COMMAND_TEXT_FILE"' EXIT

if [ -n "$COMMAND_FILE" ]; then
  cat "$COMMAND_FILE" > "$COMMAND_TEXT_FILE"
else
  printf '%s' "$COMMAND_LITERAL" > "$COMMAND_TEXT_FILE"
fi

# --rawfile reads the command text directly from disk rather than via a
# shell variable/argv (--arg), which would blow past the OS ARG_MAX limit
# once command text grows into the hundreds of KB (e.g. base64-embedding
# many scripts in one SSM command).
jq -n --rawfile cmd "$COMMAND_TEXT_FILE" '{commands: [$cmd]}' > "$PARAMS_FILE"

COMMAND_ID=$(aws ssm send-command \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --parameters "file://$PARAMS_FILE" \
  --region "$REGION" \
  --query "Command.CommandId" --output text)

log "Dispatched command $COMMAND_ID to $INSTANCE_ID (timeout ${TIMEOUT_SECONDS}s, poll every ${POLL_INTERVAL_SECONDS}s)"

ELAPSED=0
STATUS="InProgress"
while [ "$ELAPSED" -lt "$TIMEOUT_SECONDS" ]; do
  sleep "$POLL_INTERVAL_SECONDS"
  ELAPSED=$((ELAPSED + POLL_INTERVAL_SECONDS))
  RESULT=$(aws ssm get-command-invocation \
    --command-id "$COMMAND_ID" \
    --instance-id "$INSTANCE_ID" \
    --region "$REGION" \
    --output json 2>/dev/null) || continue
  STATUS=$(echo "$RESULT" | jq -r '.Status')
  case "$STATUS" in
    Success|Failed|Cancelled|TimedOut) break ;;
    *) continue ;;
  esac
done

if [ "$STATUS" = "InProgress" ] || [ "$STATUS" = "Pending" ]; then
  echo "command_id=$COMMAND_ID"
  echo "status=POLL_TIMEOUT"
  echo "exit_status="
  echo "---STDOUT---"
  echo "---STDERR---"
  log "ERROR: command $COMMAND_ID did not reach a terminal state within ${TIMEOUT_SECONDS}s (last observed status: $STATUS)"
  exit 2
fi

EXIT_STATUS=$(echo "$RESULT" | jq -r '.ResponseCode // empty')
STDOUT_CONTENT=$(echo "$RESULT" | jq -r '.StandardOutputContent')
STDERR_CONTENT=$(echo "$RESULT" | jq -r '.StandardErrorContent')

echo "command_id=$COMMAND_ID"
echo "status=$STATUS"
echo "exit_status=$EXIT_STATUS"
echo "---STDOUT---"
echo "$STDOUT_CONTENT"
echo "---STDERR---"
echo "$STDERR_CONTENT"

if [ "$STATUS" != "Success" ]; then
  log "ERROR: command $COMMAND_ID finished with status $STATUS (exit $EXIT_STATUS)"
  exit 3
fi

log "Command $COMMAND_ID completed successfully"
