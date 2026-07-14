# Riva Snap Architecture

Riva Snap is the food and water scanning model for the Riva GLP-1 companion app. You take one photo, and it tells you the dish, the portion size based on the plate, the calories, and the nutrients. It is tuned for US foods and grounded in USDA data.

It is built as a stateless pipeline service. There is no auth and no database. This is intentional, so the model can be tested and tuned on its own before we integrate it into the iOS app.

## 1. System context

```mermaid
flowchart LR
    subgraph Clients
        PHONE["Phone browser<br/>mobile web tester (web/)"]
        IOS["Riva iOS app<br/>Snap to Scan Food (future)"]
    end

    subgraph SVC["scan-service, FastAPI (stateless)"]
        WEB["GET /<br/>static tester page"]
        API["POST /v1/scan"]
        HZ["GET /healthz"]
    end

    subgraph Providers["External providers"]
        LLM["Vision LLM<br/>Groq Llama-4 Scout today<br/>(switches to OpenAI when a key is present)"]
        FDC["USDA FoodData Central<br/>search API"]
    end

    DB[("Supabase Postgres<br/>nutrition_days / food_entries<br/>(future, no writes today)")]

    PHONE --> WEB
    PHONE --> API
    IOS -. "after validation gate" .-> API
    API --> LLM
    API --> FDC
    API -. "nutrition_day_delta<br/>(contract only)" .-> DB
```

The important idea here: the service never writes to a database, but its response already matches the Riva database schema. The `nutrition_days` table stores integer calories, protein, carbs, fiber, and water in ounces. The scan response returns a `nutrition_day_delta` block with exactly those fields. When the model is accurate enough, the backend can consume it as is. Integration becomes plumbing, not a redesign.

## 2. Scan pipeline

```mermaid
flowchart TD
    A["Client sends photo + mode (auto, food, water) + optional hint"]
    A --> B["1. Preprocess (preprocess.py)<br/>fix EXIF rotation, downscale to 1024px, JPEG q85"]
    B --> C["2. Vision (vision.py)<br/>one LLM call: versioned prompt + strict JSON schema"]
    C --> D{"scan_type"}
    D -- "food or beverage" --> E["3. USDA grounding (grounding.py + fdc.py)<br/>parallel FDC search per item, score candidates,<br/>on match: per-100g values x grams"]
    D -- "water" --> W["water block only:<br/>glasses, fl oz, ml"]
    D -- "not_food" --> N["rejection with a reason"]
    E --> F["4. Assemble (main.py)"]
    W --> F
    N --> F
    F --> G["Response: items (MATCHED or AI estimate), totals,<br/>nutrition_day_delta, mode_mismatch, latency, prompt_version"]
```

What each stage does and why:

1. **Preprocess.** Phones send huge photos. Fixing the rotation and downscaling to 1024px keeps each scan at roughly one cent and a few seconds, without hurting recognition.
2. **Vision.** The model has two real jobs: name the foods and estimate the portions. The prompt tells it to use the plate or container size as a measuring reference and to assume US serving sizes. It also outputs nutrition numbers, but those are only a fallback. Structured output mode guarantees parseable JSON, and there is a retry path for providers that reject strict schemas.
3. **Grounding.** This is where accuracy comes from. For each solid food item, the service searches USDA FoodData Central and scores the candidates. A match means the nutrients are recomputed from lab-measured per-100g values times the estimated grams. That is what the MATCHED badge means. The scoring has guards we learned from live testing: it penalizes wrong food forms like flour, dry, or babyfood (but not "raw", which is the correct form for fresh produce), and it prefers entries whose first word is the food itself, so "Oranges, raw" beats "Sherbet, orange". Lookups run in parallel. If USDA is down, the scan still works with model estimates.
4. **Assemble.** Rounds everything to the database units, adds up totals, computes the delta, and flags a mode mismatch if the photo does not match what the user chose to log. One product rule lives here: only plain water fills `water_ounces`. A latte counts as calories, not hydration.

## 3. A scan, end to end

```mermaid
sequenceDiagram
    participant P as Phone (tester)
    participant S as scan-service
    participant V as Vision LLM (Groq)
    participant F as USDA FDC

    P->>S: POST /v1/scan (image, mode=food)
    S->>S: preprocess (rotate, resize, re-encode)
    S->>V: image + prompt + strict schema
    V-->>S: scan_type, items, water
    par one lookup per solid item
        S->>F: foods/search "grilled chicken breast"
        F-->>S: candidates with per-100g nutrients
    end
    S->>S: score matches, compute totals and delta, check mode mismatch
    S-->>P: ScanResponse JSON (debug adds raw output and FDC candidates)
    P->>P: show result card with MATCHED badge, Calories, Protein, Edit and Accept
```

## 4. Modes and the mismatch edge case

The mode selector (Auto, Food, Water) is a hint about intent. It never forces an interpretation.

We learned this the hard way. When the prompt told the model "the user intends to log food", it invented an entire chicken and rice dinner from a photo of a water glass. So now:

- Food mode adds no bias to what the model sees. Water mode only asks it to pay extra attention to container volume.
- The server compares what was actually detected against the mode the user picked, and sets `mode_mismatch` when they disagree. The tester shows a "Heads up" banner but renders the real content.
- Accept always logs reality. A burger scanned in Water mode logs as food calories, never as water ounces, and the reverse is also true. A beverage in Water mode also counts as a mismatch, because only plain water counts toward the hydration goal.

## 5. Module map

```mermaid
flowchart LR
    CFG["config.py<br/>.env settings"] --> MAIN
    MAIN["main.py<br/>routes, assembly, mismatch"] --> PRE["preprocess.py"]
    MAIN --> VIS["vision.py<br/>provider factory, model resolve,<br/>strict schema with fallback"]
    MAIN --> GRO["grounding.py<br/>match scoring and scaling"]
    GRO --> FDCC["fdc.py<br/>pooled FDC client"]
    MAIN --> SCH["schemas.py<br/>DB-aligned response models"]
    VIS --> PR["prompts/scan_vN.md<br/>(versioned tuning surface)"]
    EVAL["eval/run_eval.py<br/>golden set to metrics report"] --> VIS
    EVAL --> MAIN
    WEBT["web/index.html<br/>mobile tester"] --> MAIN
```

## 6. Tuning surfaces and how quality is measured

Every knob that affects accuracy is explicit, and every scan reports which prompt version and model produced it, so improvements are attributable.

| Surface | Where | Measured by |
|---|---|---|
| Prompt | `prompts/scan_vN.md`, version echoed in every response | eval report deltas |
| Vision model | `RIVA_SCAN_MODEL` env var, or the provider preference list | eval report deltas |
| Match threshold, form penalties, category bonus | constants in `grounding.py` | FDC match rate, calorie error |
| Portion calibration cues | prompt rules (plate size, US portions, ice displacement) | grams and calorie error |

`eval/run_eval.py` runs the pipeline over the photos in `eval/images/` against the labels in `golden.jsonl` and reports dish-name match rate, calorie error (MAPE), scan-type accuracy, USDA match rate, and latency percentiles.

The acceptance gate for iOS integration: at least 80% name match, at most 25% calorie MAPE, at least 95% scan-type accuracy, at least 60% USDA match rate, and p95 latency under 6 seconds.

## 7. Design principles

- **Stateless now, database-shaped always.** No persistence until the bridge-program schema settles, but the response contract already speaks the schema's language.
- **One SDK, two providers.** Groq and OpenAI both work through the OpenAI SDK and the same Chat Completions call. The provider is decided by which key exists in `.env`.
- **Grounded numbers beat clever numbers.** The LLM identifies and measures. USDA prices the nutrients whenever a match exists, and the UI is honest about which path produced each item.
- **Fail soft.** A USDA outage, a rejected schema, or an unreadable image degrades to a usable answer or a clear error. It never becomes a silently wrong log.
- **Everything observable.** Per-stage latency, per-candidate match scores, and raw model output behind a debug flag. Tuning decisions are made from evidence, not vibes.
