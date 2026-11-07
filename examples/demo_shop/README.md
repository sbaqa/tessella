# Mosaic Market: the demo shop

A small tile shop with the features of a classic practice store (test personas, sorting,
a cart, a three-step checkout), a smoother interface, **five planted bugs** and a full
Playwright suite (API and UI) built on page objects. It is
Tessella's test target, demo and benchmark: run the suite, point Tessella at the reports,
and check that it sorts every failure correctly and finds the bugs the suite misses.

- **Backend:** Starlette (the toolkit FastAPI is built on) on uvicorn, with in-memory data.
- **Frontend:** one HTML page, no build step: sign-in, patterns (sort, search), product page,
  cart (quantities, undo), checkout (details, overview with discount, thank-you), my orders,
  a side menu (all patterns, my orders, about, reset app state, log out). Cart, sign-in and
  sort order survive a reload; dark mode, phone layout and keyboard navigation work.
- **Tests:** everything on Playwright: API tests on its request context, UI tests with
  pytest-playwright, an Allure report with a screenshot per UI test, a Playwright trace per
  UI test. See [The test suite](#the-test-suite).
- **Logs:** every API request goes to `logs/app.log`, with the full traceback when a request
  crashes. Tessella reads this file as backend evidence.

## The planted bugs

| # | Bug | Kind | Does the suite catch it? | How Tessella should find it |
|---|-----|------|--------------------------|-----------------------------|
| 1 | Cancelling a paid, discounted order crashes with a 500 | Product bug | Yes: `test_cancel_paid_discounted_order_refunds_in_full` (API) and `test_cancel_discounted_order` (UI) fail | Triage: groups both failures into one Case, cites the `KeyError` in `logs/app.log` and the red error toast in the screenshot |
| 2 | `test_login` clicks `#login`, but the button is now `#sign-in` | **Test bug** | Yes: the test fails (it predates the page objects and uses raw selectors) | Triage: calls it a test bug (selector not found, the app works), drafts a patch for the test, no product ticket |
| 3 | Any signed-in user can read any order by ID | Product bug (security) | No | Probe: identity mutation, replays anna's `GET /api/orders/{id}` as bob |
| 4 | Paying the same order twice in parallel charges twice | Product bug (race) | No | Probe: concurrency mutation, fires the pay request in parallel and checks the charges |
| 5 | Cancelling a shipped order returns 409, but the order is cancelled anyway | Product bug (state) | No: the test checks the 409, not the order afterwards | Probe: state oracle, reads the order back after a rejected request |

There is also one **flaky** test, `test_recommendations_respond_quickly`. The
recommendations service takes 0 to 1.2 s and the test allows 0.8 s, so it passes about
two runs in three. Tessella's re-run step should label it flaky, not a bug.

### Switching bugs off

Bugs 1, 3, 4 and 5 live in `shop/store.py`, each behind `bugs.active(n)`. Turn them off
with an environment variable on the shop process:

```bash
DEMO_SHOP_FIXED=all  uvicorn shop.app:app --port 8765   # every product bug fixed
DEMO_SHOP_FIXED=1,4  uvicorn shop.app:app --port 8765   # only bugs 1 and 4 fixed
```

Bug 2 is in the test itself (`tests/ui/test_sign_in.py`), so fixing it means editing the
selector, which is exactly the patch Tessella should draft.

## Test accounts and personas

`anna` / `anna-pass` and `bob` / `bob-pass` are the suite's customers; their shop behaves
normally. Six personas share the password `mosaic-pass`, each with a quirk that appears only
when you sign in as it, so the planted bugs above stay exactly as they are:

| Persona | What it does |
|---|---|
| `standard` | Everything works |
| `locked` | Cannot sign in: "Sorry, this user has been locked out" |
| `problem` | One image on every pattern, sorting does nothing, some Add buttons do nothing, the last name will not type |
| `slow` | Signing in takes 2.5 s |
| `error` | Removing from the cart and placing an order fail with an error |
| `visual` | A tile is rotated, a button and the cart badge are out of place, one price is wrong |

## The test suite

Everything runs on Playwright: API tests use its request context, UI tests use
pytest-playwright. Tests never touch URLs or selectors directly; they go through a small
framework in `tests/framework/`, each part with one job:

| Part | Job |
|---|---|
| `data.py` | Accounts, products, delivery details, prices: test data by meaning |
| `api/session.py` | `ApiSession`: the transport (base URL, token), nothing else |
| `api/clients.py` | `AuthApi`, `CatalogueApi`, `OrdersApi`: one client per API area, returning responses, never asserting; `OrderSteps` builds orders in a given state |
| `pages/components.py` | `Header`, `SideMenu`, `Toast`: parts every screen shares |
| `pages/screens.py` | One page object per screen (`LoginPage`, `InventoryPage`, `CartPage`, `CheckoutInfoPage`, …); actions that change screen return the next page object |
| `flows.py` | `Shopper`: journeys across screens (sign in, fill the cart, buy) |
| `conftest.py` | Fixtures that hand these to tests (dependency injection) |

Adding a screen means a new page object and fixture, not edits to existing tests. The
default run (`pytest`) covers auth, catalogue and orders over the API and every screen in
the browser. Two markers keep extra tests out of it, so the default run's failures are
exactly the planted ones:

```bash
pytest -m planted     # 4 tests that expose bugs 1, 3, 4, 5 (fail while the bugs are on)
pytest -m persona     # 7 tests that expose the persona quirks (fail on purpose)
```

## Run it yourself, step by step

The commands assume the repository is at `~/Documents/Projects/tessella` and its virtual
environment is `venv/` in the repository root, as set up in the main README's Getting
started. Adjust the paths if yours differ. You need two terminals: one runs the shop, the
other runs everything else.

### Step 1: Install (once)

```bash
cd ~/Documents/Projects/tessella
source venv/bin/activate
pip install -e ".[all,dev]"            # Tessella itself, if you have not done it yet

cd examples/demo_shop
pip install -r requirements.txt        # starlette, uvicorn, pytest, allure-pytest, pytest-playwright
playwright install chromium            # the browser the UI tests drive (about 150 MB)
```

Check it worked:

```bash
python -c "import starlette, uvicorn, allure, pytest_playwright; print('ok')"
```

You'll also need the [Allure commandline](https://allurereport.org/docs/install/) (`allure
generate`/`allure open`) — `run_suite.sh` calls it directly, so it must be on your `PATH`.

On a fresh Linux machine Chromium may refuse to start because system libraries are
missing. `playwright install-deps chromium` installs them (it asks for sudo).

### Step 2: Start the shop (terminal 1)

```bash
cd ~/Documents/Projects/tessella/examples/demo_shop
source ../../venv/bin/activate
uvicorn shop.app:app --port 8765
```

Leave it running. Data lives in memory, so stopping the shop (Ctrl+C) empties it. Every
API request is logged to `logs/app.log`.

### Step 3: See bug 1 in the browser

Open <http://127.0.0.1:8765> and:

1. Sign in as `anna` / `anna-pass` (or select `anna` under **Test accounts**).
2. Click **Add to cart** on Marble Arabesque, open **Cart** and press **+** once (2 sheets).
3. **Checkout**, fill in your details, **Continue**.
4. Type `TILE25` into **Discount code** and **Apply**. The total drops to $37.35.
5. **Place order**. The thank-you page shows order 1001, already paid.
6. Open the menu, **My orders**, and click **Cancel order**. A red message says the server
   answered 500, and the order shows *cancelled* with no refund: the crash happened halfway
   through the cancel.

Then look at what the backend saw (terminal 2):

```bash
cd ~/Documents/Projects/tessella/examples/demo_shop
tail -n 25 logs/app.log                # ends with KeyError: 'TILE25' in shop/store.py
```

An order without a discount code cancels fine and shows its refund, which is the clue a
good investigation should find.

### Step 4: See bugs 3, 4 and 5 from the command line (terminal 2)

```bash
bash show_bugs.sh
```

It logs in as anna and bob with curl and prints the expected answer next to the actual one:

```
Bug 3: bob reads anna's order
  expected: HTTP 404
  actual:   {"id":1002,"owner":"anna",...}  (HTTP 200)

Bug 4: four parallel payments for one order
  expected: one 200, three 409, one charge
  pay -> HTTP 200   (four times)
  charges:  [{"order_id":1003,"amount":2940}, ... four charges]

Bug 5: cancelling a shipped order
  expected: HTTP 409 and the order stays shipped
  actual:   {"error":"shipped orders cannot be cancelled"}  (HTTP 409)
  order is now: cancelled
```

Read the script if you want the exact requests; each bug is a few curl calls.

### Step 5: Run the test suite (terminal 2)

```bash
bash run_suite.sh
```

The tests use the shop from step 2 if it is running, and start their own on port 8765 if
it is not. They empty the shop before each test, so the orders you made in the browser
disappear. A run takes under a minute.

Expected: **95 tests, 3 failed** (bug 1 in the API test and the UI test, bug 2 in
`test_login`). About one run in three, `test_recommendations_respond_quickly` fails too;
that is the flaky test doing its job. A run takes about three minutes.

`run_suite.sh` writes:

| Path | What it is |
|------|------------|
| `reports/allure-results/` | Raw Allure results, one JSON per test (with a screenshot attachment per UI test) |
| `reports/allure-report/` | The generated Allure report (`allure generate`), with history carried across runs for flaky-rate/trend charts |
| `reports/junit.xml` | JUnit XML, for CI |
| `test-results/` | A Playwright trace per UI test (the flows Tessella probes), plus the screenshots as PNG files |
| `logs/app.log` | The shop's request log, including the traceback for bug 1 |

Look at the results. You have two options:

```bash
# Option A: the raw Allure report (everything, with filters: use the pass/fail
# icons at the top to show only failed/broken tests first)
allure open reports/allure-report

# Option B (recommended): skip straight to Tessella's queue once you've triaged
# (see Step 7 below) — it only ever shows failures, nothing to filter
tessella triage && tessella queue
```

Allure's report is a general-purpose test report, built for CI history/trends across many
runs. Tessella's queue is your failure triage inbox — every case shown there already is a
failure, so there's no "show failures first" step needed. Prefer Option B day to day;
reach for Option A when you want the trend/history view across many runs.

In the Allure report, open `test_cancel_discounted_order`: its screenshot shows the red
error message. Some useful variations:

```bash
pytest tests/ui/test_sign_in.py::test_login -q             # one test
pytest tests/ui -k cancel --headed --slowmo 500             # watch the browser
pytest tests/api -q                                         # API tests only
```

### Step 6: Switch the bugs off and on

Stop the shop in terminal 1 (Ctrl+C) and start it with the product bugs fixed:

```bash
DEMO_SHOP_FIXED=all uvicorn shop.app:app --port 8765
```

Then, in terminal 2:

```bash
bash run_suite.sh          # now only test_login fails (bug 2 lives in the test)
bash show_bugs.sh          # every "actual" matches "expected"
pytest -m planted          # 4 passed: the proof tests for bugs 1, 3, 4, 5
```

If no shop is running, put the variable on the test run instead, since the tests start
the shop in-process: `DEMO_SHOP_FIXED=all bash run_suite.sh`. To fix only some bugs, list
them: `DEMO_SHOP_FIXED=1,4`. Restart the shop without the variable to bring the bugs back,
and run `bash run_suite.sh` once more so the reports show the failures again.

`tests/planted/` holds one proof test per product bug. They are left out of the normal run
(they are not part of a realistic suite); with the bugs on, `pytest -m planted` shows 4
failures. Use them to check the shop, not to help Tessella: bugs 3, 4 and 5 only count if
Tessella finds them without these tests.

### Step 7: Run Tessella on the results (terminal 2)

Run Tessella from this folder so it picks up this folder's `tessella.yaml`. That file reads
`reports/allure-report` (not `junit.xml`, which holds the same failures and would make
duplicate Cases), the Playwright traces and `logs/app.log`; re-runs each failure three
times into `.tessella/rerun-results` (so the traces in `test-results/` survive); and allows
network access only to the shop and a local Ollama.

Start without a model, to see collecting and clustering on their own:

```bash
tessella triage --no-model --no-rerun
```

Expect a table of 3 failures in 3 clusters (4 if the flaky test failed this run). The two
bug 1 failures stay in separate clusters at this stage, because they fail with different
messages in different files; recognising them as one bug is the investigation's job. The
*Screens* column shows the screenshot each UI failure carries.

Add re-runs, which separate the flaky test from real failures (about a minute):

```bash
tessella triage --no-model
```

Then with a local model for verdicts, if you have Ollama:

```bash
ollama pull qwen3.5:4b                 # reads logs and screenshots; ~3 GB, fine on a CPU laptop
tessella doctor                        # model roles, data policy, network rules
tessella triage
tessella queue                         # review at http://localhost:8484
```

On a CPU-only laptop expect about a minute per model step. The config turns off the
model's thinking phase and caps each group of failures at 4 steps; the main README's
"Choosing models" section has bigger options.

In the queue, approve, edit, reject or delete each draft. Nothing is filed anywhere until
you approve it.

Finally, the probe planner:

```bash
export A_PASS=anna-pass B_PASS=bob-pass
tessella probe
```

It reads the traces, links the order ID across steps and prints the mutations it plans
for each flow. Replaying them against the shop is v2 (see below).

### Step 8: Start over

```bash
rm -rf reports test-results logs       # results of the last run
rm -rf .tessella                       # Tessella's memory of past decisions, re-run output
```

Restarting the shop empties its data. The shop and tests never write outside this folder.

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| `address already in use` on port 8765 | A shop is already running. Use it, or stop it: `fuser -k 8765/tcp` |
| Tests pass that should fail (or the reverse) | The tests reuse whatever shop is on port 8765; check how it was started (`DEMO_SHOP_FIXED`) |
| You need another port | `uvicorn shop.app:app --port 9000`, then `DEMO_SHOP_URL=http://127.0.0.1:9000 bash run_suite.sh` and `SHOP=http://127.0.0.1:9000 bash show_bugs.sh`; change `probe.base_url` and `network.allow` in `tessella.yaml` too |
| `Executable doesn't exist` from Playwright | `playwright install chromium` inside the activated venv |
| `ModuleNotFoundError: No module named 'shop'` | Run pytest from `examples/demo_shop`; its `pytest.ini` adds the folder to the path |
| `tessella: command not found` | Activate the venv: `source ../../venv/bin/activate` |
| The shop's font looks plain | It loads Bricolage Grotesque from Google Fonts and falls back to a system font offline. Tests do not depend on it |

## What Tessella does with it today (v0.1)

- **Triage** reads `reports/allure-report` with its screenshots, clusters failures by
  signature (normalised message plus failing line), re-runs each failure three times (which
  separates the flaky test), and asks the reader model for a verdict that may cite only
  collected evidence.
- **Probe** reads the Playwright traces, links IDs across steps (the order ID from
  `POST /api/orders` into `/pay` and `/cancel`) and lists the mutations it would try,
  including the identity swap and parallel pay that expose bugs 3 and 4. Replaying them
  against the live shop is v2, so today the planted proof tests stand in for that step.
- `logs/app.log` is already written in a form Tessella's file log adapter reads; wiring
  log evidence into the triage pipeline is on the v1 list.

## What a complete run should produce (the benchmark target)

1. **One** product-bug draft for bug 1 that joins the API and UI failures. Its evidence
   cites the `KeyError: 'TILE25'` traceback from `logs/app.log`, the 500 response, and the
   screenshot with the red "Couldn't cancel order" message.
2. A test-bug verdict for `test_login`, with a draft patch changing `#login` to `#sign-in`
   (or to `get_by_role("button", name="Sign in")`). No product ticket.
3. A flaky verdict for the recommendations test (passed on some re-runs, failed on others).
4. Three probe findings, each with a minimal request sequence that reproduces it: bob
   reading anna's order (bug 3), several charges for one order (bug 4), and a shipped
   order that ends up cancelled after a 409 (bug 5). The UI never ships an order, so bug 5
   also tests whether probe can extend a flow with the `/ship` endpoint it saw nowhere
   else; expect it to be the hardest of the five.

Definition of done: all five found, the test bug and the flaky test labelled as such, and
fewer than 10 false positives across the probe run.

## Layout

```
demo_shop/
├── shop/
│   ├── app.py          routes, request log
│   ├── store.py        orders, payments, refunds; bugs 1, 3, 4, 5
│   ├── bugs.py         DEMO_SHOP_FIXED switch
│   └── static/index.html
├── tests/
│   ├── conftest.py     starts the shop if needed, resets it per test, fixtures, screenshots
│   ├── framework/      data, API clients, page objects, the Shopper flow
│   ├── api/            auth, catalogue (the flaky test), orders (bug 1)
│   ├── ui/             sign-in (bug 2), patterns, cart, checkout, orders (bug 1), menu, personas
│   └── planted/        proof tests, run with -m planted
├── pytest.ini
├── requirements.txt
├── run_suite.sh       runs the suite, writes reports/
├── show_bugs.sh       reproduces bugs 1, 3, 4, 5 with curl
└── tessella.yaml
```

The accounts (`anna` / `anna-pass`, `bob` / `bob-pass`, the personas with `mosaic-pass`)
and the `/api/test/reset` endpoint exist only for this demo.
