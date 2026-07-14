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
