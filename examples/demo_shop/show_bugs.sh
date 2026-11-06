#!/usr/bin/env bash
# Reproduce planted bugs 1, 3, 4 and 5 by hand with curl, one at a time.
#
#   bash show_bugs.sh                        # against http://127.0.0.1:8765
#   SHOP=http://127.0.0.1:9000 bash show_bugs.sh
#
# Needs a running shop (uvicorn shop.app:app --port 8765) plus curl and python3.
# Each bug prints what a correct shop would answer next to what this one answers.
set -euo pipefail
SHOP=${SHOP:-http://127.0.0.1:8765}

if ! curl -sf "$SHOP/api/health" >/dev/null; then
  echo "No shop at $SHOP. Start it first: uvicorn shop.app:app --port 8765" >&2
  exit 1
fi

json() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }
token() {
  curl -s "$SHOP/api/auth/login" -H 'content-type: application/json' \
    -d "{\"username\":\"$1\",\"password\":\"$1-pass\"}" | json '["token"]'
}
# new_order [extra JSON fields]: anna orders 3 sheets of Terracotta Square
new_order() {
  curl -s "$SHOP/api/orders" -H "authorization: Bearer $ANNA" -H 'content-type: application/json' \
    -d "{\"items\":[{\"product_id\":2,\"qty\":3}]${1:-}}" | json '["id"]'
}
post() { curl -s -X POST "$SHOP/api/orders/$1/$2" -H "authorization: Bearer $3" "${@:4}"; }

ANNA=$(token anna)
BOB=$(token bob)

echo
echo "Bug 1: cancelling a paid, discounted order"
echo "  expected: HTTP 200 and a full refund"
ID=$(new_order ',"discount_code":"TILE25"')
post "$ID" pay "$ANNA" >/dev/null
echo -n "  actual:   "; post "$ID" cancel "$ANNA" -w "  (HTTP %{http_code})\n"
echo "  a 500 leaves its traceback in logs/app.log"

echo
echo "Bug 3: bob reads anna's order"
echo "  expected: HTTP 404"
ID=$(new_order)
echo -n "  actual:   "; curl -s "$SHOP/api/orders/$ID" -H "authorization: Bearer $BOB" -w "  (HTTP %{http_code})\n"

echo
echo "Bug 4: four parallel payments for one order"
echo "  expected: one 200, three 409, one charge"
ID=$(new_order)
for _ in 1 2 3 4; do
  post "$ID" pay "$ANNA" -o /dev/null -w "  pay -> HTTP %{http_code}\n" &
done
wait
echo -n "  charges:  "; curl -s "$SHOP/api/orders/$ID/charges" -H "authorization: Bearer $ANNA"; echo

echo
echo "Bug 5: cancelling a shipped order"
echo "  expected: HTTP 409 and the order stays shipped"
ID=$(new_order)
post "$ID" pay "$ANNA" >/dev/null
post "$ID" ship "$ANNA" >/dev/null
echo -n "  actual:   "; post "$ID" cancel "$ANNA" -w "  (HTTP %{http_code})\n"
echo -n "  order is now: "; curl -s "$SHOP/api/orders/$ID" -H "authorization: Bearer $ANNA" | json '["status"]'
echo
