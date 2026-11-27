#!/usr/bin/env bash
#
# Create the QStash cron schedules that replace the in-process ticker.
#
# The task list, the intervals and the endpoint are all taken from the code, not
# invented here: the names must match app/api/internal/cron.py's
# LOCAL_CRON_INTERVALS and the URL must be the same host the app advertises as
# PUBLIC_BASE_URL, because the route's internal-token guard is what authenticates
# the call.
#
# Idempotent by construction: every schedule is created with a fixed
# `Upstash-Schedule-Id`, and QStash upserts when that ID already exists. Re-run
# it after an interval changes and the existing schedule is replaced rather than
# duplicated.
#
# Usage:
#   QSTASH_TOKEN=... INTERNAL_TOKEN=... PUBLIC_BASE_URL=https://... \
#     ./deploy/qstash-schedules.sh
#   ./deploy/qstash-schedules.sh --list      # show what exists, change nothing
#   ./deploy/qstash-schedules.sh --delete    # remove every schedule this owns

set -euo pipefail

TASKS=(
  "growth_dispatch:*/5 * * * *"
  "rollup:*/15 * * * *"
  "recrawl:0 * * * *"
  "checkpoint_expiry:0 * * * *"
  "approval_expiry:0 * * * *"
  "growth_report_due:0 * * * *"
  "growth_store_sync:0 * * * *"
  "growth_crm_retry:0 * * * *"
  "growth_indiamart_poll:0 * * * *"
  "growth_insights_due:0 6 * * *"
  "email_retention:0 3 * * *"
)

MODE=create
while [[ $# -gt 0 ]]; do
  case "$1" in
    --list) MODE=list; shift ;;
    --delete) MODE=delete; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# QSTASH_URL is region-scoped and a token only works against its own region's
# host. The app defaults to the global host, which routes to EU — so an EU token
# works with the default and a US token needs https://qstash-us-east-1.upstash.io.
# Read the same variable the app reads so the two cannot disagree.
QSTASH_URL="${QSTASH_URL:-https://qstash.upstash.io/v2}"
QSTASH_URL="${QSTASH_URL%/}"
[[ "$QSTASH_URL" == */v2 ]] || QSTASH_URL="$QSTASH_URL/v2"

API="$QSTASH_URL/schedules"
prefix="kado-cron"

if [[ "$MODE" == list || "$MODE" == delete ]]; then
  : "${QSTASH_TOKEN:?QSTASH_TOKEN is required}"
else
  : "${QSTASH_TOKEN:?QSTASH_TOKEN is required}"
  : "${INTERNAL_TOKEN:?INTERNAL_TOKEN is required — it is what authenticates the cron route}"
  : "${PUBLIC_BASE_URL:?PUBLIC_BASE_URL is required — the service origin, without a trailing slash}"
  PUBLIC_BASE_URL="${PUBLIC_BASE_URL%/}"
  if [[ "$PUBLIC_BASE_URL" == *localhost* || "$PUBLIC_BASE_URL" == *127.0.0.1* ]]; then
    echo "PUBLIC_BASE_URL is not publicly reachable — QStash cannot call it." >&2
    exit 1
  fi
fi

curl_qstash() {
  curl --silent --show-error --write-out '\n%{http_code}' "$@"
}

if [[ "$MODE" == list ]]; then
  curl_qstash -H "Authorization: Bearer $QSTASH_TOKEN" "$API" | sed '$d' |
    python3 -c '
import json,sys
try:
    rows = json.load(sys.stdin)
except Exception:
    print("could not parse the schedule list — check the token and region host")
    raise SystemExit(0)
print(f"{len(rows)} schedule(s):")
for r in rows:
    print(f"  {r.get(\"scheduleId\",\"?\"):<34} {r.get(\"cron\",\"?\"):<18} {r.get(\"destination\",\"?\")}")
'
  exit 0
fi

if [[ "$MODE" == delete ]]; then
  body=$(curl_qstash -H "Authorization: Bearer $QSTASH_TOKEN" "$API" | sed '$d')
  ids=$(printf '%s' "$body" | python3 -c '
import json,sys
try:
    rows = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
for r in rows:
    sid = r.get("scheduleId", "")
    if sid.startswith("kado-"):
        print(sid)
')
  if [[ -z "$ids" ]]; then
    echo "nothing to delete"
    exit 0
  fi
  while read -r sid; do
    [[ -n "$sid" ]] || continue
    code=$(curl_qstash -X DELETE -H "Authorization: Bearer $QSTASH_TOKEN" "$API/$sid" | tail -n1)
    # the API documents only a 200 and a description string; there is no body
    # schema, so success is the status code and nothing else is parsed
    echo "deleted $sid (HTTP $code)"
  done <<< "$ids"
  exit 0
fi

echo "destination host: $PUBLIC_BASE_URL"
echo

failed=0
for entry in "${TASKS[@]}"; do
  task="${entry%%:*}"
  cron="${entry#*:}"
  sleep_seconds=0
  case "$cron" in
    "*/5 * * * *") sleep_seconds=300 ;;
    "*/15 * * * *") sleep_seconds=900 ;;
    "0 6 * * *"|"0 3 * * *") sleep_seconds=86400 ;;
    *) sleep_seconds=3600 ;;
  esac

  response=$(curl_qstash \
    --request POST \
    --url "$API/${PUBLIC_BASE_URL}/internal/cron/${task}" \
    --header "Authorization: Bearer $QSTASH_TOKEN" \
    --header "Upstash-Cron: $cron" \
    --header "Upstash-Schedule-Id: ${prefix}-${task}" \
    --header "Upstash-Forward-x-internal-token: ${INTERNAL_TOKEN}" \
    --header "Upstash-Retries: 3" \
    --header "content-type: application/json" \
    --data '{}')

  http_code=$(printf '%s' "$response" | tail -n1)
  payload=$(printf '%s' "$response" | sed '$d')

  case "$http_code" in
    200|201)
      echo "ok    ${task}  ${cron}  (every ${sleep_seconds}s)"
      ;;
    412)
      # documented meaning: the plan's active-schedule cap is reached. The free
      # plan allows 10; this list has 11, so a free project fails on the last one
      # or two. Upgrading to Pay as You Go lifts the pool to 1,000 at no extra
      # cost until 1,000 schedules.
      echo "CAP   ${task}  — active-schedule limit reached for this plan: $payload" >&2
      failed=1
      ;;
    401|403)
      echo "AUTH  ${task}  — token rejected: $payload" >&2
      failed=1
      ;;
    *)
      echo "FAIL  ${task}  — HTTP $http_code: $payload" >&2
      failed=1
      ;;
  esac
done

echo
if [[ $failed -ne 0 ]]; then
  echo "One or more schedules were not created. See the lines above." >&2
  exit 1
fi

cat <<'EOF'
All schedules are in place. Notes:

  * A new schedule can take up to 60 seconds to load on an active node, so the
    first firing is not immediate. Confirm it in the Upstash console under
    QStash → Schedules, which shows nextScheduleTime per row.
  * Every scheduled call carries x-internal-token, which is one of the two
    credentials app/api/internal/cron.py accepts. The other is a QStash
    signature; the token path was chosen so no signing-key wiring is needed
    here, but note that signing keys must still be configured, because the
    app refuses to boot in production without them.
  * Retries: 3 per delivery. The destination's answer decides whether a retry
    counts as wasted work — it returns 200 for every task it handles, and a
    failure is retried by QStash rather than lost.
  * To pause a schedule instead of deleting it, POST to
    {QSTASH_URL}/schedules/{scheduleId}/pause and .../resume.
EOF
