# Riva Food-Scan Model — Implementation Plan

**Status:** Approved · **Phase:** Model validation (pre-iOS integration)
**Owner:** Riva engineering · **Last updated:** July 14, 2026

---

## 1. Goal & Context

Riva is a GLP-1 medication companion app (patients receive it with their medicine). It needs a
**HealthifyMe-Snap-style scanner**: the user points their camera at food or water — or uploads a
photo from the gallery — and the app identifies the dish and the plate/container, estimates the
portion, and returns **calories plus associated nutrients**, tuned for **US health seekers**
(US foods, US portion norms, USDA data).

**Strategy decided:** build and validate the scanning *model* first as a standalone service,
test it through a minimal Android app, measure and tune accuracy — and only integrate into the
production iOS app once it performs well.

> "Model" here means an **AI pipeline service** (vision LLM + nutrition-database grounding),
> not a custom-trained network. Tuning = prompt/grounding/model-choice iteration, measured by
> an eval harness. A custom-trained model is only revisited if this pipeline's ceiling proves
> insufficient.

### Key decisions (confirmed)

| Decision | Choice |
|---|---|
| Vision provider | **OpenAI** (user's available API key). Responses API, image input, strict Structured Outputs |
| Vision model | Env-configurable `RIVA_SCAN_MODEL`; verified against `/v1/models` on the account; default = best available vision model (gpt-5.x family, fallback gpt-4o) |
| Backend | **Python 3 + FastAPI** |
| Nutrition grounding | **USDA FoodData Central (FDC)** when matched (MATCHED badge); model estimate as fallback |
| Test client | **Mobile-optimized web app** served by the scan service itself (`scan-service/web/`) — any phone on the same Wi-Fi opens the Mac's LAN URL; camera + gallery via file inputs. (Pivoted from the Android-app option; a native client can still come later.) |
| Persistence | **None in this phase** — service is stateless (safe against pending bridge-program schema changes) |

### Repository layout

Both components live beside the iOS project:

```
/Users/ashutoshanand/Downloads/Riva/
├── Riva.xcodeproj / Riva/     # existing iOS app (untouched this phase)
└── scan-service/              # NEW — the model (FastAPI) + mobile web tester (web/)
```

---

## 2. Product Requirements

1. **Input:** a single photo — captured live or picked from the gallery.
2. **Detect what's in frame:**
   - Plated food (identify the dish *and* the plate/bowl/container — plate size informs portion)
   - Water / beverages (container type → volume estimate)
   - Not-food (graceful rejection with a reason)
3. **Output per food item:** name, portion description + grams, confidence, calories,
   protein, carbs, fiber (+ extended: fat, sugar, sodium), alternative guesses for one-tap
   correction, and whether values are **MATCHED** (USDA-grounded) or **AI-estimated**.
4. **Output for water/beverages:** volume in **ounces** + glasses equivalent (DB tracks ounces).
5. **US market:** US serving conventions (oz, cups, pieces), US restaurant/home portion norms,
   USDA-canonical nutrient values.
6. **Tunable:** every scan reports its `prompt_version` and latency; accuracy is measurable
   via the eval harness so iterations are compared objectively.

---

## 3. System Architecture

```
┌──────────────────┐   multipart JPEG    ┌─────────────────────────────────────────┐
│ RivaScanTester   │ ──────────────────► │ scan-service (FastAPI)                  │
│ (Android, test)  │                     │                                         │
│ capture/gallery  │ ◄────────────────── │ 1. preprocess: EXIF-orient, ≤1024px,    │
└──────────────────┘    JSON result      │    JPEG re-encode (cost/latency control)│
                                         │ 2. vision: OpenAI Responses API call    │
   (later: Riva iOS app                  │    image + versioned prompt +           │
    Snap → Scan Food)                    │    strict JSON schema                   │
                                         │ 3. grounding: USDA FDC search + match   │
                                         │    matched ⇒ per-100g × grams           │
                                         │ 4. assemble: items, totals,             │
                                         │    nutrition_day_delta, latency, debug  │
                                         └──────────┬──────────────────┬───────────┘
                                                    │                  │
                                             OpenAI API         USDA FoodData
                                             (vision LLM)       Central API
```

---

## 4. Part 1 — `scan-service/` (the model)

### 4.1 File structure

```
scan-service/
├── app/
│   ├── main.py            # FastAPI app: POST /v1/scan, GET /healthz, CORS open (dev only)
│   ├── config.py          # env: OPENAI_API_KEY, FDC_API_KEY, RIVA_SCAN_MODEL, RIVA_SCAN_DEBUG
│   ├── preprocess.py      # Pillow: EXIF transpose → downscale long edge to 1024px → JPEG q85
│   ├── vision.py          # OpenAI Responses API call; loads prompts/scan_vN.md; strict schema
│   ├── fdc.py             # FDC /v1/foods/search client + per-100g nutrient extraction (httpx)
│   ├── grounding.py       # item→FDC matching, scoring, nutrient recomputation
│   └── schemas.py         # Pydantic request/response models (DB-aligned units)
├── prompts/
│   └── scan_v1.md         # US-portion-tuned system prompt (versioned files = tuning surface)
├── eval/
│   ├── images/            # test photos (user-supplied)
│   ├── golden.jsonl       # expected labels: {file, dish, kcal, grams, scan_type}
│   ├── run_eval.py        # batch-run pipeline → eval/reports/<timestamp>.md
│   └── reports/
├── requirements.txt       # fastapi, uvicorn, openai, pillow, httpx, python-multipart, pydantic
├── .env.example
└── README.md              # run instructions + API contract for Android/iOS clients
```

### 4.2 API contract

**`GET /healthz`** → `{"status": "ok", "model": "<resolved model>", "prompt_version": "v1"}`

**`POST /v1/scan`** — `multipart/form-data`
- `image` (required): JPEG/PNG/HEIC photo
- `hint` (optional): free-text context, e.g. `"dinner at Chipotle"`
- `debug` (optional bool): include raw model output + stage latencies

Response (example):

```json
{
  "scan_type": "food",
  "plate": "white ceramic dinner plate, ~10.5 in",
  "items": [
    {
      "name": "Grilled chicken breast",
      "portion_desc": "1 breast (~6 oz)",
      "portion_grams": 170,
      "confidence": "high",
      "calories": 280,
      "protein_grams": 53,
      "carb_grams": 0,
      "fiber_grams": 0,
      "extended": { "fat_g": 6.1, "sugar_g": 0.0, "sodium_mg": 126 },
      "matched": true,
      "fdc_id": 171477,
      "alternatives": ["Grilled turkey breast", "Baked chicken thigh"]
    }
  ],
  "water": null,
  "totals": { "calories": 280, "protein_grams": 53, "carb_grams": 0, "fiber_grams": 0 },
  "nutrition_day_delta": {
    "calories": 280, "protein_grams": 53, "carb_grams": 0,
    "fiber_grams": 0, "water_ounces": 0
  },
  "prompt_version": "v1",
  "model": "gpt-5.1",
  "latency_ms": { "total": 3400, "vision": 2900, "grounding": 380 }
}
```

Water scan example: `"scan_type": "water"`, `"water": {"container_type": "glass",
"volume_oz": 12, "glasses": 1.5}`, `nutrition_day_delta.water_ounces = 12`.
Not-food: `"scan_type": "not_food"` + `"reason"`; items empty; delta all zeros.

### 4.3 Pipeline stages

1. **Preprocess** (`preprocess.py`): honor EXIF orientation, downscale longest edge to
   1024px, re-encode JPEG q85. Controls token cost (~$0.01/scan) and keeps latency 2–4s.
2. **Vision** (`vision.py`): one OpenAI Responses API call — image + system prompt from
   `prompts/scan_vN.md` + **strict Structured Output schema** (no free-text parsing).
   Prompt requirements: US portions (oz/cups/pieces), plate-aware portion reasoning,
   identify every distinct food on the plate, water/beverage volume from container type,
   2 alternatives when confidence < high, `not_food` escape hatch.
3. **Grounding** (`fdc.py` + `grounding.py`): for each item, query FDC search
   (`api.nal.usda.gov/fdc/v1/foods/search`), prefer data types
   `Foundation > SR Legacy > Survey (FNDDS)`, fuzzy-score candidate descriptions vs the
   item name; accept above threshold ⇒ recompute nutrients = per-100g values × grams/100,
   set `matched: true` + `fdc_id`. No acceptable match ⇒ keep model estimate,
   `matched: false`. FDC nutrient IDs: energy 1008, protein 1003, carbs 1005, fiber 1079,
   fat 1004, sugars 2000, sodium 1093.
4. **Assemble**: integer-round DB-aligned fields, compute totals + `nutrition_day_delta`,
   attach latency breakdown, `prompt_version`, model id; raw vision output only when `debug`.

### 4.4 Database alignment (per `FINAL_DATABASE_SCHEMA.md`)

- The app's persisted nutrition state is **`nutrition_days`**
  (`calories`, `protein_grams`, `carb_grams`, `fiber_grams`, `water_ounces` — all integers)
  with goals in **`nutrition_goals`** (water goal in **ounces** — hence ounces in the API).
- **`nutrition_day_delta`** is exactly the increment set for the `nutrition_days` upsert —
  the future backend integration consumes it without translation.
- Per-item output is shaped to become rows of the future **`food_entries`** table
  (explicitly listed in the schema's future enhancements, gated on this feature).
- Extended nutrients (fat/sugar/sodium) ride in a nested `extended` block since the DB
  doesn't persist them yet.
- Service does **no DB writes** this phase → immune to the minor bridge-program schema
  changes the user will make; alignment is contract-level only. Revisit the contract once
  the updated schema lands.

### 4.5 Configuration & keys

`.env` (from `.env.example`):
- `OPENAI_API_KEY` — required (user has this)
- `FDC_API_KEY` — DONE: real key in `.env`, verified live against the FDC search API
- `RIVA_SCAN_MODEL` — optional override; otherwise resolved from `/v1/models` at startup
- `RIVA_SCAN_DEBUG` — default-on debug payloads during this phase

---

## 5. Part 2 — Eval harness (how "fine-tuning" is measured)

- **Golden set:** user drops 30–50 photos into `eval/images/` covering: home-cooked plates,
  US restaurant/chain meals, packaged foods, mixed dishes (salads, bowls, curries),
  water/beverages in varied containers, and a few non-food negatives. Labels in
  `eval/golden.jsonl`: `{file, dish, kcal, grams, scan_type}`.
- **`run_eval.py`** batch-runs the pipeline and writes `eval/reports/<timestamp>.md` with:
  - **Dish-name match rate** — fuzzy match of top-1 name (or any alternative) vs golden dish
  - **Calorie MAPE** — mean absolute percentage error vs golden kcal
  - **FDC match rate** — % of items grounded to USDA
  - **scan_type accuracy** — food/water/not-food classification
  - **Latency** p50 / p95, and cost/scan estimate
- **Tuning loop:** observe failures → write `prompts/scan_v2.md` (or change
  `RIVA_SCAN_MODEL`) → re-run eval → compare reports side by side. Every report records the
  prompt version + model, so progress is attributable.

### Acceptance targets (gate for iOS integration)

| Metric | Target |
|---|---|
| Dish-name match (top-1 or alternative) | ≥ 80% |
| Calorie MAPE | ≤ 25% |
| scan_type accuracy | ≥ 95% |
| FDC match rate | ≥ 60% |
| Latency p95 (end-to-end) | ≤ 6 s |

---

## 6. Part 3 — Mobile web tester (`scan-service/web/`)

A single self-contained mobile-first page **served by the FastAPI service itself**
(same origin — no base-URL config, no app store, works on any phone on the Wi-Fi):

- **📷 Capture** (`<input accept="image/*" capture="environment">`) and **🖼 Gallery** pick
- Optional **hint** field ("dinner at Chipotle"), image preview with a scanning shimmer
  and the "… detected" pill from the approved scanner wireframe
- Result card per the wireframe: item name, portion, **✓ MATCHED / AI ESTIMATE** badge,
  **Calories + Protein tiles** (fiber intentionally not displayed per design; the API
  still returns `fiber_grams` because `nutrition_days` tracks it), "Not X?" alternative
  chips, and **Edit / Accept** actions (Accept previews the `nutrition_day_delta` that
  would be logged)
- Water/beverage card (oz + glasses), plate totals for multi-item scans, latency + model
  + prompt-version footer, expandable **raw JSON** panel (the tuning view)
- Riva design language, light + dark via `prefers-color-scheme`
- Phone usage: `http://<Mac LAN IP>:8000` (`ipconfig getifaddr en0`). Note: iOS Safari
  allows camera input on plain HTTP for same-network testing via file inputs; if a
  browser restricts it, the Gallery path always works.

---

## 7. Milestones

| # | Milestone | Deliverable / gate |
|---|---|---|
| M1 | Scan service skeleton | `/healthz` up; `/v1/scan` returns schema-valid JSON for a test image |
| M2 | FDC grounding | MATCHED items recompute from USDA per-100g values; fallback path clean |
| M3 | Eval harness | Report generates on a seed set; baseline metrics recorded |
| M4 | Web tester | Phone opens LAN URL; capture/gallery scan renders end-to-end |
| M5 | Tuning rounds | Prompt v2+ iterations; metrics vs acceptance targets |
| M6 | Go/No-Go | Targets met ⇒ plan iOS integration (Snap → Scan Food) + backend persistence (`food_entries`, `nutrition_days` upsert) |

## 8. Verification (end-to-end)

1. `uvicorn app.main:app` → `curl /healthz`; `curl -F image=@<photo> localhost:8000/v1/scan`
   → inspect items / calories / `matched` / `nutrition_day_delta`.
2. Seed `eval/images`, run `python eval/run_eval.py`, confirm a timestamped report.
3. Gradle-build the APK, boot the emulator, install via adb, scan a gallery image against
   the Mac's LAN IP, confirm the full result card renders.
4. Make one prompt change (v1 → v2) and show the eval report delta — proving the tuning loop.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Portion estimation error dominates calorie error | Plate-aware prompting; report grams separately in eval; alternatives + (later) in-app portion adjust |
| FDC search returns poor matches for composed dishes | Data-type preference order + fuzzy threshold; fall back to model estimate rather than a bad match |
| Bridge-program schema changes | Service stateless; only the DTO mirrors the DB — one small contract review when schema lands |
| OpenAI model availability/pricing shifts | `RIVA_SCAN_MODEL` env switch; startup verification against `/v1/models`; model recorded per scan/eval |
| LAN testing friction (phone ↔ Mac) | Base-URL field in tester app; CORS open in dev; same-Wi-Fi requirement documented in README |
| Emulator camera is synthetic | Gallery-upload path on emulator; physical device for camera UX |

## 10. Out of scope (this phase)

- iOS integration (`SnapAction.food` stays a placeholder until Go decision)
- Auth, rate limiting, cloud deployment, DB writes
- Barcode/UPC scanning (strong v2 candidate for packaged US foods)
- Literal model fine-tuning/training — revisit only if the pipeline misses targets after tuning

## 11. Future (post-validation) — iOS integration sketch

- `ScanRepository` protocol in the iOS app's `Core/Repositories` (same pattern as
  Home/Medication/Tracker/Profile) → `APIScanRepository` hitting this service
- Snap → Scan Food opens the camera flow; result card mirrors the tester app's layout in the
  Riva design system; `nutrition_day_delta` feeds the `nutrition_days` upsert via the backend
- Add `NSCameraUsageDescription` + photo-library usage strings at that point
