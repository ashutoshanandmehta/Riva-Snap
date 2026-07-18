# Riva Snap (backend)

My backend for Riva: take a photo of food or water and get back the dish,
the portion, the calories, and the nutrients, plus the logging APIs behind
every quick-log flow in the app (weight, shots, protein, side effects,
sleep). Tuned for US foods and grounded in USDA FoodData Central. The
system design, diagrams, and tuning surfaces are in `ARCHITECTURE.md`.

## Run

```sh
cd backend
uv venv --python 3.12 .venv          # uv avoids my broken Homebrew Python 3.14
uv pip install -r requirements.txt --python .venv/bin/python
# fill .env from .env.example first (Groq or OpenAI key, FDC key, Supabase)
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Smoke test:

```sh
curl http://localhost:8000/healthz
```

## Mobile web tester

The service serves my phone-optimized tester at its root. On any phone on
the same Wi-Fi:

```
http://<my Mac's LAN IP>:8000        # ipconfig getifaddr en0
```

Capture or pick a photo, Scan, review the result card (MATCHED badge,
Calories + Protein, alternatives, Edit/Accept), with a raw JSON panel for
tuning. When Supabase is configured the tester asks for an email code
sign-in and Accept really logs.

## Deploy

Render runs this service at https://riva-snap.onrender.com via the
`render.yaml` blueprint. Deploys come from the Riva-Snap mirror repo that
Render watches; I sync it whenever this folder changes. Secrets live in
Render env vars, never in the repo.

Notes:

- The free plan sleeps after about 15 minutes of no traffic. The first
  request after that takes 30 to 60 seconds to wake the service.
- Scanning and logging require a session, so strangers with the URL cannot
  spend the LLM quota.

## API

All endpoints below except `/healthz`, `/v1/config`, and
`/v1/device/session` require `Authorization: Bearer <access token>` once
Supabase is configured. Without Supabase env vars the service runs in an
open stateless mode: scanning works, logging is disabled.

### `GET /healthz`

Resolved vision model, prompt version, and key presence.

### `GET /v1/config`

Client bootstrap: whether the backend is enabled, plus the Supabase URL
and anon key for the web tester's login (public by design).

### `POST /v1/device/session` (JSON)

Interim identity while the product has no sign-in screen. The app sends a
stable random `device_id` and gets a session for a silently provisioned
account (synthetic email, password derived server side from the service
role key). Replaced by the real sign-in when the landing page ships.

### `POST /v1/scan` (multipart/form-data)

| field | type | notes |
|---|---|---|
| `image` | file | JPEG/PNG/HEIC photo (required) |
| `mode`  | text | `auto` (default), `food`, or `water`: intent hint, never a filter |
| `hint`  | text | optional context, e.g. "dinner at Chipotle" |
| `debug` | bool | include raw model output + FDC query candidates |

Response essentials:

- `scan_type`: `food` | `water` | `beverage` | `not_food`
- `plate`: container description used for portion calibration
- `items[]`: name, `portion_desc`, `portion_grams`, confidence, integer
  calories/protein/carbs/fiber, `extended` (fat/sugar/sodium), `matched` +
  `fdc_id` when USDA-grounded, `alternatives` for one-tap correction
- `water`: container type, `volume_oz`, `volume_ml`, 8-oz `glasses`
- `nutrition_day_delta`: the exact increments for the `nutrition_days`
  upsert. Product rule: only plain water fills `water_ounces`; beverages
  contribute calories and macros instead.
- `mode_mismatch`: true when the photo disagrees with the chosen mode; the
  delta always reflects the actual content
- `latency` per stage; `model` and `prompt_version` for tuning attribution

### `POST /v1/log` (JSON)

Saves an accepted scan (or a manual protein add): one `food_entries`
history row plus the `nutrition_days` increment in one transaction.
Returns the updated day totals.

### `POST /v1/log/weight` (JSON)

`{pounds}` inserts a `weights` row, snapshotting the current dose from the
active medication plan for trend analysis.

### `POST /v1/log/shot` (JSON)

`{medication_name, dose_mg, injection_site, comfort_rating?}` inserts a
`shots` row and syncs `medication_plans.current_dose_mg` in the same
transaction (creating the plan on first shot). Sites: right_arm, left_arm,
lower_left_abs, lower_right_abs, right_thigh, left_thigh.

### `POST /v1/log/side-effects` (JSON)

`{effects: [{effect, severity}]}` replaces today's set in
`side_effect_logs` + items. Effects: nausea, headache, fatigue,
constipation, diarrhea, dizziness, bloating, heartburn, food_noise;
severity 1 to 5.

### `POST /v1/log/checkin` (JSON)

`{question_id, option_code}` answers one of today's check-in questions
(seeded: mood, energy, sleep, nausea, appetite). The app's sleep quality
sheet uses `question_id: "sleep"`.

### `GET /v1/me`

Everything the Profile tab needs in one call: the profile row, nutrition
goals, health goal flags, and the active medication plan (null when the
user has none yet).

### `POST /v1/profile` (JSON)

Any subset of name, date_of_birth, gender, clinician_name, start_weight,
goal_weight, height_inches, timezone. Returns the full updated profile.

### `POST /v1/goals` (JSON)

Any subset of protein_goal, carb_goal, fiber_goal, water_goal (each 0 to
2000). Returns the updated goals.

### `POST /v1/plan` (JSON)

Any subset of name, current_dose_mg, cadence_days, reminder_description.
Updates the active medication plan, creating one with sensible defaults
on first use. Returns the full plan.

### `GET /v1/weights?limit=60`

Weight entries newest first, soft deletes excluded. Limit 1 to 200.

### `GET /v1/shots?limit=60`

Shot history newest first, soft deletes excluded. Limit 1 to 200.

### `GET /v1/side-effects?days=30`

Daily side effect logs for the window, newest first, each day carrying
its effects and severities plus the note. Days 1 to 90.

### `GET /v1/export`

One JSON object with every row the user owns, for data portability.

### `DELETE /v1/account`

Deletes the auth user via the admin API; every table cascades. Returns
`{"deleted": true}`.

## Backend (Supabase)

Set `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `SUPABASE_SERVICE_ROLE_KEY`
in `.env` (and on Render). Apply the migrations from
`supabase/migrations/` in order via the dashboard SQL Editor:

1. `0001_nutrition.sql`: profiles, goals, nutrition_days, food_entries,
   the signup provisioning trigger, and `log_scan`.
2. `0002_logging.sql`: medication_plans, shots, weights, side effects,
   check-ins with seeded questions, and the `log_*` functions.

Design rules I follow throughout: server-authoritative writes (clients
only authenticate; all writes go through SECURITY DEFINER functions
callable only by the service role), Row Level Security on every user
table, soft deletes, and timezone-aware calendar days.

## Tuning loop

1. Drop photos into `eval/images/`, label them in `eval/golden.jsonl`
   (see `golden.example.jsonl`). Aim for 30 to 50 photos covering home
   plates, restaurant meals, packaged foods, mixed dishes, water and
   other drinks in varied containers, and a few non-food negatives.
2. `.venv/bin/python eval/run_eval.py` writes a report to `eval/reports/`.
3. Edit `prompts/scan_v2.md` + bump `prompt_version` in `app/config.py`
   (or change `RIVA_SCAN_MODEL` in `.env`), re-run, compare reports.

My acceptance targets: dish-name match at least 80%, calorie MAPE at most
25%, scan_type accuracy at least 95%, FDC match at least 60%, p95 latency
under 6 seconds.

## Configuration (`.env`)

| var | meaning |
|---|---|
| `GROQ_API_KEY` | vision LLM (Llama 4 Scout); active provider today |
| `OPENAI_API_KEY` | optional, switches the provider to OpenAI when set |
| `FDC_API_KEY` | USDA FoodData Central key (`DEMO_KEY` for smoke tests) |
| `RIVA_SCAN_MODEL` | optional model override; empty = auto-resolve best available |
| `RIVA_SCAN_DEBUG` | default debug payloads on/off |
| `SUPABASE_URL` + `SUPABASE_ANON_KEY` + `SUPABASE_SERVICE_ROLE_KEY` | enable sessions and persistent logging (all three or none) |
