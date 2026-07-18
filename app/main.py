"""Riva Scan Service — food/water photo → dish, portion, calories, nutrients.

Stateless by design in this phase: no auth, no DB. The response contract
mirrors the Riva database schema (see FOOD_SCAN_MODEL_PLAN.md).
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from . import backend, grounding, preprocess, vision
from .config import settings
from .schemas import (
    GENDERS,
    INJECTION_SITES,
    SIDE_EFFECTS,
    AccountDeleteResult,
    BackendConfig,
    CheckinLogRequest,
    CheckinLogResult,
    DayTotals,
    DeviceSession,
    DeviceSessionRequest,
    ExtendedNutrients,
    GoalsUpdateRequest,
    GoalsUpdateResult,
    HealthResponse,
    LatencyBreakdown,
    LogRequest,
    MeResponse,
    NutritionDayDelta,
    PlanUpdateRequest,
    PlanUpdateResult,
    ProfileUpdateRequest,
    ProfileUpdateResult,
    ScanDebug,
    ScanItem,
    ScanResponse,
    ShotListResult,
    ShotLogRequest,
    ShotLogResult,
    SideEffectItem,
    SideEffectListResult,
    SideEffectsLogRequest,
    SideEffectsLogResult,
    Totals,
    WaterResult,
    WeightListResult,
    WeightLogRequest,
    WeightLogResult,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scan")

app = FastAPI(title="Riva Scan Service", version="0.1.0")

# Dev-only: the Android tester (and later iOS dev builds) call from the LAN.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_client: OpenAI | None = None
_provider: str | None = None
_model: str | None = None


def _llm() -> tuple[OpenAI, str]:
    """Lazily builds the provider client and resolves the model once."""
    global _client, _provider, _model
    if _client is None:
        config = settings()
        try:
            client, provider = vision.make_client(config)
            model = vision.resolve_model(client, provider, config.riva_scan_model)
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        _client, _provider, _model = client, provider, model
        logger.info("Vision provider: %s, model: %s", provider, model)
    assert _model is not None
    return _client, _model


def _authenticate(authorization: str | None) -> str | None:
    """Gate for API calls. Returns the user id when the backend is
    configured; None in open stateless mode (no Supabase env set)."""
    config = settings()
    if not backend.is_configured(config):
        return None
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return backend.verify_token(config, authorization.split(" ", 1)[1])


@app.get("/v1/config", response_model=BackendConfig)
def client_config() -> BackendConfig:
    """Bootstrap info for the web tester (anon key is public by design)."""
    config = settings()
    enabled = backend.is_configured(config)
    return BackendConfig(
        backend_enabled=enabled,
        supabase_url=config.supabase_url or None if enabled else None,
        supabase_anon_key=config.supabase_anon_key or None if enabled else None,
    )


@app.post("/v1/device/session", response_model=DeviceSession)
def device_session(request: DeviceSessionRequest) -> DeviceSession:
    """Interim no-sign-in identity: the app sends its stable device id and
    gets a session for a silently provisioned account. Replaced by the real
    landing page sign-in later.
    """
    config = settings()
    if not backend.is_configured(config):
        raise HTTPException(
            status_code=503,
            detail="Device accounts need the Supabase backend. Set SUPABASE_URL and keys in .env.",
        )
    device_id = request.device_id.strip()
    if not (8 <= len(device_id) <= 64) or not all(c.isalnum() or c == "-" for c in device_id):
        raise HTTPException(status_code=400, detail="device_id must be 8 to 64 letters, digits, or dashes.")
    return DeviceSession(**backend.device_session(config, device_id))


@app.post("/v1/log", response_model=DayTotals)
def log(request: LogRequest, authorization: str | None = Header(default=None)) -> DayTotals:
    """Persists an accepted scan and returns the updated day totals."""
    config = settings()
    user_id = _authenticate(authorization)
    if user_id is None:
        raise HTTPException(
            status_code=503,
            detail="Logging needs the Supabase backend. Set SUPABASE_URL and keys in .env.",
        )
    if request.scan_type not in ("food", "beverage", "water"):
        raise HTTPException(status_code=400, detail="Nothing loggable in this scan.")
    totals = backend.log_scan(config, user_id, request.model_dump())
    return DayTotals(**totals)


def _require_user(authorization: str | None) -> str:
    """Auth for the logging endpoints, which cannot run in open mode."""
    user_id = _authenticate(authorization)
    if user_id is None:
        raise HTTPException(
            status_code=503,
            detail="Logging needs the Supabase backend. Set SUPABASE_URL and keys in .env.",
        )
    return user_id


@app.post("/v1/log/weight", response_model=WeightLogResult)
def log_weight(
    request: WeightLogRequest, authorization: str | None = Header(default=None)
) -> WeightLogResult:
    user_id = _require_user(authorization)
    if not (20 <= request.pounds <= 1500):
        raise HTTPException(status_code=400, detail="Enter a weight between 20 and 1500 lbs.")
    row = backend.log_weight(settings(), user_id, round(request.pounds, 2), request.measured_at)
    return WeightLogResult(**row)


@app.post("/v1/log/shot", response_model=ShotLogResult)
def log_shot(
    request: ShotLogRequest, authorization: str | None = Header(default=None)
) -> ShotLogResult:
    user_id = _require_user(authorization)
    if not request.medication_name.strip():
        raise HTTPException(status_code=400, detail="Enter the medication name.")
    if not (0 < request.dose_mg <= 100):
        raise HTTPException(status_code=400, detail="Enter a dose between 0 and 100 mg.")
    if request.injection_site not in INJECTION_SITES:
        raise HTTPException(status_code=400, detail="Unknown injection site.")
    if request.comfort_rating is not None and not (1 <= request.comfort_rating <= 5):
        raise HTTPException(status_code=400, detail="Comfort rating is 1 to 5.")
    entry = request.model_dump()
    entry["medication_name"] = request.medication_name.strip()
    row = backend.log_shot(settings(), user_id, entry)
    return ShotLogResult(**row)


@app.post("/v1/log/side-effects", response_model=SideEffectsLogResult)
def log_side_effects(
    request: SideEffectsLogRequest, authorization: str | None = Header(default=None)
) -> SideEffectsLogResult:
    user_id = _require_user(authorization)
    for item in request.effects:
        if item.effect not in SIDE_EFFECTS:
            raise HTTPException(status_code=400, detail=f"Unknown side effect: {item.effect}")
        if not (1 <= item.severity <= 5):
            raise HTTPException(status_code=400, detail="Severity is 1 to 5.")
    rows = backend.log_side_effects(
        settings(), user_id,
        [item.model_dump() for item in request.effects],
        request.note,
    )
    return SideEffectsLogResult(
        log_date=rows[0]["log_date"] if rows else None,
        effects=[SideEffectItem(effect=r["effect"], severity=r["severity"]) for r in rows],
    )


@app.post("/v1/log/checkin", response_model=CheckinLogResult)
def log_checkin(
    request: CheckinLogRequest, authorization: str | None = Header(default=None)
) -> CheckinLogResult:
    user_id = _require_user(authorization)
    row = backend.log_checkin(
        settings(), user_id, request.question_id.strip(), request.option_code.strip()
    )
    return CheckinLogResult(**row)


@app.get("/v1/me", response_model=MeResponse)
def me(authorization: str | None = Header(default=None)) -> MeResponse:
    """Profile, nutrition goals, health goals, and the active plan."""
    user_id = _require_user(authorization)
    return MeResponse(**backend.get_me(settings(), user_id))


@app.post("/v1/profile", response_model=ProfileUpdateResult)
def update_profile(
    request: ProfileUpdateRequest, authorization: str | None = Header(default=None)
) -> ProfileUpdateResult:
    user_id = _require_user(authorization)
    fields = request.model_dump(exclude_unset=True)
    if "name" in fields:
        if not (fields["name"] or "").strip():
            raise HTTPException(status_code=400, detail="Enter a name.")
        fields["name"] = fields["name"].strip()
    if "timezone" in fields:
        if not (fields["timezone"] or "").strip():
            raise HTTPException(status_code=400, detail="Enter a timezone.")
        fields["timezone"] = fields["timezone"].strip()
    if fields.get("gender") is not None and fields["gender"] not in GENDERS:
        raise HTTPException(status_code=400, detail="Unknown gender option.")
    if fields.get("date_of_birth") is not None:
        try:
            date.fromisoformat(fields["date_of_birth"])
        except ValueError as error:
            raise HTTPException(
                status_code=400, detail="Date of birth must be YYYY-MM-DD."
            ) from error
    for key, label in (("start_weight", "start weight"), ("goal_weight", "goal weight")):
        value = fields.get(key)
        if value is not None and not (20 <= value <= 1500):
            raise HTTPException(
                status_code=400, detail=f"Enter a {label} between 20 and 1500 lbs."
            )
    height = fields.get("height_inches")
    if height is not None and not (20 <= height <= 120):
        raise HTTPException(status_code=400, detail="Enter a height between 20 and 120 inches.")
    profile = backend.update_profile(settings(), user_id, fields)
    return ProfileUpdateResult(profile=profile)


@app.post("/v1/goals", response_model=GoalsUpdateResult)
def update_goals(
    request: GoalsUpdateRequest, authorization: str | None = Header(default=None)
) -> GoalsUpdateResult:
    user_id = _require_user(authorization)
    fields = request.model_dump(exclude_unset=True)
    for value in fields.values():
        if value is None or not (0 <= value <= 2000):
            raise HTTPException(status_code=400, detail="Enter a goal between 0 and 2000.")
    goals = backend.update_goals(settings(), user_id, fields)
    return GoalsUpdateResult(nutrition_goals=goals)


@app.post("/v1/plan", response_model=PlanUpdateResult)
def update_plan(
    request: PlanUpdateRequest, authorization: str | None = Header(default=None)
) -> PlanUpdateResult:
    user_id = _require_user(authorization)
    fields = request.model_dump(exclude_unset=True)
    if "name" in fields:
        if not (fields["name"] or "").strip():
            raise HTTPException(status_code=400, detail="Enter the medication name.")
        fields["name"] = fields["name"].strip()
    if "current_dose_mg" in fields and (
        fields["current_dose_mg"] is None or not (0 < fields["current_dose_mg"] <= 100)
    ):
        raise HTTPException(status_code=400, detail="Enter a dose between 0 and 100 mg.")
    if "cadence_days" in fields and (
        fields["cadence_days"] is None or not (1 <= fields["cadence_days"] <= 90)
    ):
        raise HTTPException(status_code=400, detail="Cadence is 1 to 90 days.")
    plan = backend.upsert_plan(settings(), user_id, fields)
    return PlanUpdateResult(plan=plan)


@app.get("/v1/weights", response_model=WeightListResult)
def list_weights(
    limit: int = 60, authorization: str | None = Header(default=None)
) -> WeightListResult:
    user_id = _require_user(authorization)
    rows = backend.list_weights(settings(), user_id, max(1, min(limit, 200)))
    return WeightListResult(entries=rows)


@app.get("/v1/shots", response_model=ShotListResult)
def list_shots(
    limit: int = 60, authorization: str | None = Header(default=None)
) -> ShotListResult:
    user_id = _require_user(authorization)
    rows = backend.list_shots(settings(), user_id, max(1, min(limit, 200)))
    return ShotListResult(entries=rows)


@app.get("/v1/side-effects", response_model=SideEffectListResult)
def list_side_effects(
    days: int = 30, authorization: str | None = Header(default=None)
) -> SideEffectListResult:
    user_id = _require_user(authorization)
    logs = backend.list_side_effects(settings(), user_id, max(1, min(days, 90)))
    return SideEffectListResult(logs=logs)


@app.get("/v1/export")
def export_data(authorization: str | None = Header(default=None)) -> JSONResponse:
    """The user's complete data as one JSON object, for portability."""
    user_id = _require_user(authorization)
    return JSONResponse(backend.export_user(settings(), user_id))


@app.delete("/v1/account", response_model=AccountDeleteResult)
def delete_account(authorization: str | None = Header(default=None)) -> AccountDeleteResult:
    user_id = _require_user(authorization)
    backend.delete_account(settings(), user_id)
    return AccountDeleteResult(deleted=True)


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    config = settings()
    model: str | None = None
    if config.openai_api_key or config.groq_api_key:
        try:
            model = _llm()[1]
        except HTTPException:
            model = None
    return HealthResponse(
        status="ok",
        provider=_provider,
        model=model,
        prompt_version=config.prompt_version,
        llm_key_present=bool(config.openai_api_key or config.groq_api_key),
        fdc_key_present=bool(config.fdc_api_key),
    )


@app.post("/v1/scan", response_model=ScanResponse, response_model_exclude_none=True)
def scan(
    image: UploadFile = File(...),
    hint: str | None = Form(default=None),
    mode: str = Form(default="auto"),
    debug: bool | None = Form(default=None),
    authorization: str | None = Header(default=None),
) -> ScanResponse:
    if mode not in ("auto", "food", "water"):
        raise HTTPException(status_code=400, detail="mode must be auto, food, or water")
    # When the backend is configured, scanning requires sign-in. This also
    # keeps the public Render URL from burning the shared LLM quota.
    _authenticate(authorization)
    config = settings()
    include_debug = config.riva_scan_debug if debug is None else debug
    started = time.monotonic()

    raw = image.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty image upload.")

    # 1. Preprocess
    try:
        image_b64 = preprocess.prepare_image(raw)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Unreadable image: {error}") from error
    preprocess_done = time.monotonic()

    # 2. Vision
    client, model = _llm()
    prompt_text = vision.load_prompt(config.prompt_version)
    try:
        analysis = vision.analyze_image(
            client, model, image_b64, hint, prompt_text,
            provider=_provider or "groq", mode=mode,
        )
    except Exception as error:
        logger.exception("Vision call failed")
        raise HTTPException(status_code=502, detail=f"Vision model error: {error}") from error
    vision_done = time.monotonic()

    # 3. USDA grounding + 4. assembly
    result = _assemble(analysis, config.fdc_api_key)
    grounding_done = time.monotonic()

    result.requested_mode = mode
    result.mode_mismatch = _is_mismatch(mode, result.scan_type)
    result.prompt_version = config.prompt_version
    result.model = model
    result.latency = LatencyBreakdown(
        total_ms=int((grounding_done - started) * 1000),
        preprocess_ms=int((preprocess_done - started) * 1000),
        vision_ms=int((vision_done - preprocess_done) * 1000),
        grounding_ms=int((grounding_done - vision_done) * 1000),
    )
    if include_debug:
        result.debug = ScanDebug(
            raw_model_output=analysis,
            fdc_queries=result.debug.fdc_queries if result.debug else [],
        )
    else:
        result.debug = None
    return result


def _is_mismatch(mode: str, scan_type: str) -> bool:
    """True when the photo's content disagrees with what the user chose to log.

    `not_food` is its own rejection state, not a mismatch. A beverage under
    food mode is acceptable (it has nutrition to log); a beverage under water
    mode IS a mismatch — only plain water counts toward the hydration goal.
    """
    if mode == "water":
        return scan_type in ("food", "beverage")
    if mode == "food":
        return scan_type == "water"
    return False


def _assemble(analysis: dict, fdc_api_key: str) -> ScanResponse:
    """Grounds items against USDA and builds the DB-aligned response."""
    scan_type = analysis.get("scan_type", "not_food")
    raw_items = analysis.get("items", [])
    # Plain water carries no nutrition — the water block is the whole result,
    # so drop any zero-value item entries the model produced for it.
    if scan_type == "water":
        raw_items = []
    items: list[ScanItem] = []
    fdc_debug: list[dict] = []

    # Ground all items concurrently — sequential FDC lookups dominate latency
    # on multi-item plates (~1.5s each).
    def _lookup(raw_item: dict):
        if scan_type != "water" and not raw_item.get("is_liquid", False):
            return grounding.best_match(fdc_api_key, raw_item["name"])
        return None, []

    with ThreadPoolExecutor(max_workers=8) as pool:
        lookups = list(pool.map(_lookup, raw_items))

    for raw_item, (candidate, query_debug) in zip(raw_items, lookups):
        fdc_debug.extend(query_debug)
        grams = float(raw_item.get("portion_grams", 0))
        estimate = {
            "calories": float(raw_item.get("calories", 0)),
            "protein_g": float(raw_item.get("protein_g", 0)),
            "carb_g": float(raw_item.get("carb_g", 0)),
            "fiber_g": float(raw_item.get("fiber_g", 0)),
            "fat_g": float(raw_item.get("fat_g", 0)),
            "sugar_g": float(raw_item.get("sugar_g", 0)),
            "sodium_mg": float(raw_item.get("sodium_mg", 0)),
        }

        if candidate is not None:
            grounded = grounding.grounded_nutrients(candidate, grams)
            # USDA values win where present; model fills the gaps.
            nutrients = {**estimate, **grounded}
            matched, fdc_id = True, candidate["fdc_id"]
            fdc_description, source = candidate["description"], "usda"
        else:
            nutrients = estimate
            matched, fdc_id, fdc_description, source = False, None, None, "model"

        items.append(
            ScanItem(
                name=raw_item["name"],
                portion_desc=raw_item.get("portion_desc", ""),
                portion_grams=grams,
                confidence=raw_item.get("confidence", "low"),
                calories=round(nutrients["calories"]),
                protein_grams=round(nutrients["protein_g"]),
                carb_grams=round(nutrients["carb_g"]),
                fiber_grams=round(nutrients["fiber_g"]),
                extended=ExtendedNutrients(
                    fat_g=round(nutrients["fat_g"], 1),
                    sugar_g=round(nutrients["sugar_g"], 1),
                    sodium_mg=round(nutrients["sodium_mg"], 1),
                ),
                matched=matched,
                fdc_id=fdc_id,
                fdc_description=fdc_description,
                source=source,
                alternatives=list(raw_item.get("alternatives", []))[:2],
            )
        )

    totals = Totals(
        calories=sum(i.calories for i in items),
        protein_grams=sum(i.protein_grams for i in items),
        carb_grams=sum(i.carb_grams for i in items),
        fiber_grams=sum(i.fiber_grams for i in items),
    )

    water_raw = analysis.get("water")
    water = None
    if water_raw:
        volume_oz = round(float(water_raw.get("volume_oz", 0)))
        water = WaterResult(
            container_type=water_raw.get("container_type", "container"),
            volume_oz=volume_oz,
            volume_ml=round(volume_oz * 29.5735),
            glasses=round(float(water_raw.get("glasses", 0)), 1),
        )

    # Product rule: only plain water counts toward the daily water goal;
    # beverages contribute calories/macros instead.
    water_ounces = water.volume_oz if (water and scan_type == "water") else 0

    return ScanResponse(
        scan_type=scan_type,
        requested_mode="auto",  # filled by caller
        mode_mismatch=False,  # filled by caller
        reason=analysis.get("reason"),
        plate=analysis.get("plate"),
        items=items,
        water=water,
        totals=totals,
        nutrition_day_delta=NutritionDayDelta(
            calories=totals.calories,
            protein_grams=totals.protein_grams,
            carb_grams=totals.carb_grams,
            fiber_grams=totals.fiber_grams,
            water_ounces=water_ounces,
        ),
        prompt_version="",  # filled by caller
        model="",  # filled by caller
        latency=LatencyBreakdown(total_ms=0, preprocess_ms=0, vision_ms=0, grounding_ms=0),
        debug=ScanDebug(raw_model_output={}, fdc_queries=fdc_debug),
    )


# Mobile web tester — served by the API itself so the phone needs only the
# Mac's LAN address (same origin, no base-URL config).
app.mount(
    "/",
    StaticFiles(directory=Path(__file__).resolve().parent.parent / "web", html=True),
    name="web",
)
