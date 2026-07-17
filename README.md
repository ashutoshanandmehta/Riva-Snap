# Riva Scan Service

Take a photo of food or water and get back the dish, the portion, the
calories, and the nutrients. Tuned for US foods and grounded in USDA
FoodData Central. See `ARCHITECTURE.md` for the system design, diagrams, tuning
surfaces, and the accuracy gate for iOS integration.

## Run

```sh
cd scan-service
uv venv --python 3.12 .venv          # uv avoids the broken Homebrew Python 3.14
uv pip install -r requirements.txt --python .venv/bin/python
# add OPENAI_API_KEY to .env first
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Smoke test:

```sh
curl http://localhost:8000/healthz
curl -F image=@path/to/food-photo.jpg http://localhost:8000/v1/scan | python3 -m json.tool
```

## Deploy on Render

The repo ships a `render.yaml` blueprint. In the Render dashboard choose
New, then Blueprint, pick this repo, and Render reads the config and asks
for two secrets: `GROQ_API_KEY` and `FDC_API_KEY`. The service comes up at
`https://riva-snap.onrender.com` (or similar) with the web tester at the
root and the API at `/v1/scan`.

Notes:

- The free plan sleeps after about 15 minutes of no traffic. The first
  request after that takes 30 to 60 seconds to wake the service.
- The URL is public and unauthenticated in this phase, which is fine for
  tuning but means anyone with the link can spend your Groq quota. Ask for
  a simple access key gate before sharing the link widely.

## Mobile web tester

The service serves a phone-optimized tester at its root. On any phone on the
same Wi-Fi, open:

```
http://<your Mac's LAN IP>:8000        # ipconfig getifaddr en0
```

Capture or pick a photo → Scan → result card (MATCHED badge, Calories +
Protein, alternatives, Edit/Accept) + raw JSON panel for tuning.

## API

### `GET /healthz`

Reports resolved vision model, prompt version, and key presence.

### `POST /v1/scan` (multipart/form-data)

| field | type | notes |
|---|---|---|
| `image` | file | JPEG/PNG/HEIC photo (required) |
| `hint`  | text | optional context, e.g. "dinner at Chipotle" |
| `debug` | bool | include raw model output + FDC query candidates |

Response essentials:

- `scan_type`: `food` \| `water` \| `beverage` \| `not_food`
- `plate`: container description used for portion calibration
- `items[]`: name, `portion_desc`, `portion_grams`, confidence,
  `calories`/`protein_grams`/`carb_grams`/`fiber_grams` (ints, DB-aligned),
  `extended` (fat/sugar/sodium), `matched` + `fdc_id` when USDA-grounded,
  `alternatives` for one-tap correction
- `water`: container type, `volume_oz`, 8-oz `glasses`
- `nutrition_day_delta`: exact increments for the app's `nutrition_days`
  upsert (`calories`, `protein_grams`, `carb_grams`, `fiber_grams`,
  `water_ounces`). Product rule: only `scan_type == "water"` fills
  `water_ounces`; beverages contribute calories/macros instead.
- `latency`: per-stage ms; `model`, `prompt_version` for tuning attribution

### `GET /v1/config`

Client bootstrap info: whether the backend is enabled, plus the Supabase URL
and anon key for the login flow.

### `POST /v1/log` (JSON, sign-in required)

Saves an accepted scan. Body carries the scan type, items, and the delta
fields from the scan response. Writes one `food_entries` row and increments
the user's `nutrition_days` totals in one transaction, then returns the
updated day totals.

### `POST /v1/device/session` (JSON)

Interim identity for the iOS app while it has no sign-in screen. The app
sends a stable random `device_id` and gets back a session for a silently
provisioned account (synthetic email, password derived server side from the
service role key). Replaced by the real sign-in when the landing page ships.

## Backend (Supabase)

The service persists accepted logs to Supabase when three env vars are set:
`SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `SUPABASE_SERVICE_ROLE_KEY`
(Dashboard, Project Settings, API). With them set, `/v1/scan` and `/v1/log`
require a signed-in user, and the web tester shows an email code login.
Without them the service runs in the old open, stateless mode.

Setup:

1. Create a free project at supabase.com.
2. Apply `supabase/migrations/0001_nutrition.sql` (paste into the SQL Editor
   and run, or run it over the direct Postgres connection). It creates the
   nutrition tables from the Riva schema, a `food_entries` history table,
   and the `log_scan` function that does the atomic write.
3. Put the three values in `.env` (and on Render for the deployed service).

Notes:

- Sign-in is an email code (OTP). Supabase's built-in email service allows
  only a few OTP emails per hour per project, which is fine for testing.
- Writes are server-authoritative: the client only authenticates, and the
  service verifies the token and calls `log_scan` with the service role.
- Only plain water fills `water_ounces`. Beverages log calories and macros.

## Tuning loop

1. Drop photos into `eval/images/`, label them in `eval/golden.jsonl`
   (see `golden.example.jsonl`). Aim for 30 to 50 photos covering home
   plates, restaurant meals, packaged foods, mixed dishes, water and
   other drinks in varied containers, and a few non-food negatives.
2. `.venv/bin/python eval/run_eval.py` → report in `eval/reports/`.
3. Edit `prompts/scan_v2.md` + bump `prompt_version` in `app/config.py`
   (or change `RIVA_SCAN_MODEL` in `.env`), re-run, compare reports.

Acceptance targets (gate for iOS integration): dish-name match ≥ 80%,
calorie MAPE ≤ 25%, scan_type accuracy ≥ 95%, FDC match ≥ 60%, p95 ≤ 6 s.

## Configuration (`.env`)

| var | meaning |
|---|---|
| `OPENAI_API_KEY` | optional, switches the provider to OpenAI when set |
| `FDC_API_KEY` | USDA FoodData Central key (`DEMO_KEY` for smoke tests) |
| `RIVA_SCAN_MODEL` | optional model override; empty = auto-resolve best available |
| `RIVA_SCAN_DEBUG` | default debug payloads on/off |
| `SUPABASE_URL` + `SUPABASE_ANON_KEY` + `SUPABASE_SERVICE_ROLE_KEY` | enable sign-in and persistent logging (all three or none) |
