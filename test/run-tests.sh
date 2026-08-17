#!/usr/bin/env bash
# End-to-end behaviour tests against a mock /api/coins.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT=8787
SCEN=/tmp/scenario.json
STATE=/tmp/test-state.json
pass=0; fail=0
check() { if [ "$2" = "$3" ]; then echo "  PASS $1"; pass=$((pass+1)); else echo "  FAIL $1 (expected '$3', got '$2')"; fail=$((fail+1)); fi }

BASE_ENV=(DRY_RUN=true RUN_ONCE=true FETCH_ATTEMPTS=1 STATE_FILE=$STATE
          ANSEM_API_URL="http://127.0.0.1:$PORT/api/coins")

run() { env "${BASE_ENV[@]}" ALERT_ON_FIRST_RUN=true "$@" node watch.mjs 2>&1; }

coin() { # name ticker mint tier status
  printf '{"slug":"%s-x","name":"%s","ticker":"%s","mint":"%s","tier":"%s","status":"%s",' "$2" "$1" "$2" "$3" "$4" "$5"
  printf '"marketCapUsd":1480000,"volume24hUsd":240000,"change24hPct":126.4,"txns24h":8391,'
  printf '"teamPct":5,"airdropPct":10,"curvePct":100,"imageUrl":null,"description":null,'
  printf '"creatorWallet":"5hh7UE6i2BtyCvaMMadS71wFwK88JRmtRTfXkjZ3CHvF","airdropTotal":100000000,'
  printf '"pairAddress":null,"priceUsd":0.0014,"enhancedAt":null,"createdAt":"2026-08-17T16:00:00.000Z"}'
}

# scenario <coin-spec>... where each spec is "name|ticker|mint|tier|status"
scenario() {
  { printf '['
    local first=1
    for spec in "$@"; do
      IFS='|' read -r n t m tr st <<< "$spec"
      [ $first -eq 0 ] && printf ','
      first=0
      coin "$n" "$t" "$m" "$tr" "$st"
    done
    printf ']'
  } > $SCEN
  # fail fast if we generated invalid JSON
  node -e "JSON.parse(require('fs').readFileSync('$SCEN','utf8'))" || { echo "  ERROR: scenario is not valid JSON"; exit 1; }
}

echo "== starting mock api =="
echo "[]" > $SCEN
# NOTE: the mock's stdout/stderr must be redirected to a file. If it inherits the
# script's stdout and the script is piped (e.g. `| tail`), the background process
# holds the pipe open and the reader never sees EOF.
node test/mock-api.mjs $SCEN $PORT >/tmp/mock.log 2>&1 & MOCK=$!
trap 'kill $MOCK 2>/dev/null' EXIT
sleep 1

echo "== test 1: free-only feed produces no alerts =="
rm -f $STATE
scenario "Just Air|AIR|MINTfree1|free|on_curve"
out=$(run)
check "no alerts for free tier" "$(echo "$out" | grep -c 'would send')" "0"

echo "== test 2: a gold coin alerts once =="
rm -f $STATE
scenario "Hyper Bull|HBULL|MINTgold1|gold|migrated" "Just Air|AIR|MINTfree1|free|on_curve"
out=$(run)
check "one alert sent" "$(echo "$out" | grep -c 'would send')" "1"
check "alert names the ticker" "$(echo "$out" | grep -c 'HBULL')" "2"
check "alert says GOLD" "$(echo "$out" | grep -c 'GOLD')" "1"
check "labelled new listing" "$(echo "$out" | grep -c 'new listing')" "1"

echo "== test 3: re-running does not re-alert (dedupe) =="
out=$(run)
check "no duplicate alert" "$(echo "$out" | grep -c 'would send')" "0"

echo "== test 4: a diamond coin appearing later alerts =="
scenario "Hyper Bull|HBULL|MINTgold1|gold|migrated" "Black Goddess|ALYCIA|MINTdia1|diamond|migrated"
out=$(run)
check "diamond alert sent" "$(echo "$out" | grep -c 'would send')" "1"
check "alert says DIAMOND" "$(echo "$out" | grep -c 'DIAMOND')" "1"
check "gold coin not re-alerted" "$(echo "$out" | grep -c 'HBULL')" "0"

echo "== test 5: free -> gold upgrade is detected and labelled =="
rm -f $STATE
scenario "Just Air|AIR|MINTfree1|free|on_curve"
run WATCH_TIERS=free > /dev/null 2>&1          # record the coin while it is still free
scenario "Just Air|AIR|MINTfree1|gold|on_curve"
out=$(run WATCH_TIERS=free,gold,diamond)
check "upgrade alert sent" "$(echo "$out" | grep -c 'would send')" "1"
check "labelled tier upgrade" "$(echo "$out" | grep -c 'tier upgrade')" "1"

echo "== test 6: first run without ALERT_ON_FIRST_RUN stays silent =="
rm -f $STATE
scenario "Hyper Bull|HBULL|MINTgold1|gold|migrated"
out=$(env "${BASE_ENV[@]}" node watch.mjs 2>&1)
check "silent on cold start" "$(echo "$out" | grep -c 'would send')" "0"
check "but recorded it" "$(grep -c MINTgold1 $STATE)" "1"
out=$(run)
check "and does not alert later either" "$(echo "$out" | grep -c 'would send')" "0"

echo "== test 7: telegram failure rolls back state so the alert retries =="
rm -f $STATE
scenario "Hyper Bull|HBULL|MINTgold1|gold|migrated"
# point the telegram base at the mock (which never returns ok:true) to force failure
out=$(env "${BASE_ENV[@]}" ALERT_ON_FIRST_RUN=true DRY_RUN=false \
      TELEGRAM_BOT_TOKEN=x TELEGRAM_CHAT_ID=y \
      TELEGRAM_API_BASE="http://127.0.0.1:$PORT" node watch.mjs 2>&1)
check "send failure is logged" "$(echo "$out" | grep -c 'ERROR sending alert')" "1"
check "state rolled back" "$(grep -c MINTgold1 $STATE)" "0"

echo "== test 8: bad tier name is rejected =="
out=$(WATCH_TIERS=platinum DRY_RUN=true RUN_ONCE=true STATE_FILE=/tmp/none.json node watch.mjs 2>&1)
check "rejects unknown tier" "$(echo "$out" | grep -c 'unknown tier')" "1"

echo "== test 9: missing token is rejected in live mode =="
out=$(RUN_ONCE=true STATE_FILE=/tmp/none.json node watch.mjs 2>&1)
check "requires bot token" "$(echo "$out" | grep -c 'TELEGRAM_BOT_TOKEN is not set')" "1"

echo "== test 10: unreachable api does not crash the loop =="
rm -f $STATE
out=$(env DRY_RUN=true RUN_ONCE=true FETCH_ATTEMPTS=1 STATE_FILE=$STATE \
      ANSEM_API_URL="http://127.0.0.1:9/api/coins" node watch.mjs 2>&1)
check "logs error and exits cleanly" "$(echo "$out" | grep -c 'ERROR during check')" "1"

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
