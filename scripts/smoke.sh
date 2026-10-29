#!/bin/sh
# Read-only checks against a running instance (safe on a customer's live one).
#   BASE_URL=https://... ADMIN_TOKEN=... [AT_CALLBACK_TOKEN=...] sh scripts/smoke.sh
set -u
: "${BASE_URL:?set BASE_URL}" "${ADMIN_TOKEN:?set ADMIN_TOKEN}"
fail=0
check() {  # name expected_status curl-args...
  name=$1; want=$2; shift 2
  got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@")
  if [ "$got" = "$want" ]; then echo "ok   $name ($got)"; else echo "FAIL $name: got $got, want $want"; fail=1; fi
}
check "health"                 200 "$BASE_URL/health"
check "portal login page"      200 "$BASE_URL/portal/login"
check "portal needs login"     303 "$BASE_URL/portal"
check "API rejects no token"   401 "$BASE_URL/api/campaigns/0"
check "API accepts token"      404 -H "Authorization: Bearer $ADMIN_TOKEN" "$BASE_URL/api/campaigns/0"
if [ -n "${AT_CALLBACK_TOKEN:-}" ]; then
  check "callback rejects no token" 403 -X POST -d "from=%2B254700000000&text=HELP" "$BASE_URL/at/sms/inbound"
else
  echo "warn AT_CALLBACK_TOKEN not given: callback check skipped"
fi
[ $fail = 0 ] && echo PASS || { echo FAIL; exit 1; }
