"""API response models.

Units and field names mirror the Riva database schema
(`nutrition_days`: integer calories / protein_grams / carb_grams /
fiber_grams / water_ounces) so the future backend integration consumes
`nutrition_day_delta` without translation. Extended nutrients that the DB
does not persist yet ride in `ExtendedNutrients`.
"""

from pydantic import BaseModel


class ExtendedNutrients(BaseModel):
    fat_g: float
    sugar_g: float
    sodium_mg: float


class ScanItem(BaseModel):
    name: str
    portion_desc: str
    portion_grams: float
    confidence: str  # high | medium | low
    calories: int
    protein_grams: int
    carb_grams: int
    fiber_grams: int
    extended: ExtendedNutrients
    # True when nutrients were recomputed from a USDA FoodData Central match.
    matched: bool
    fdc_id: int | None
    fdc_description: str | None
    source: str  # "usda" | "model"
    alternatives: list[str]


class WaterResult(BaseModel):
    container_type: str
    volume_oz: int
    # Metric display convenience; the DB tracks ounces.
    volume_ml: int
    glasses: float


class Totals(BaseModel):
    calories: int
    protein_grams: int
    carb_grams: int
    fiber_grams: int


class NutritionDayDelta(BaseModel):
    """Increment set for the app's `nutrition_days` daily upsert."""

    calories: int
    protein_grams: int
    carb_grams: int
    fiber_grams: int
    water_ounces: int


class LatencyBreakdown(BaseModel):
    total_ms: int
    preprocess_ms: int
    vision_ms: int
    grounding_ms: int


class ScanDebug(BaseModel):
    raw_model_output: dict
    fdc_queries: list[dict]


class ScanResponse(BaseModel):
    scan_type: str  # food | water | beverage | not_food
    # What the client asked to log (auto | food | water) and whether the
    # photo's actual content disagrees — the UI should surface a gentle
    # redirect; the delta always reflects the ACTUAL content.
    requested_mode: str
    mode_mismatch: bool
    reason: str | None
    plate: str | None
    items: list[ScanItem]
    water: WaterResult | None
    totals: Totals
    nutrition_day_delta: NutritionDayDelta
    prompt_version: str
    model: str
    latency: LatencyBreakdown
    debug: ScanDebug | None = None


class BackendConfig(BaseModel):
    """Public client bootstrap info (the anon key is public by design)."""

    backend_enabled: bool
    supabase_url: str | None
    supabase_anon_key: str | None


class LogRequest(BaseModel):
    """An accepted scan, as sent back by the client for persistence."""

    scan_type: str  # food | beverage | water
    items: list[dict] = []
    calories: int = 0
    protein_grams: int = 0
    carb_grams: int = 0
    fiber_grams: int = 0
    water_ounces: int = 0
    model: str | None = None
    prompt_version: str | None = None


class DayTotals(BaseModel):
    """The user's nutrition_days row after the log was applied."""

    day: str
    calories: int
    protein_grams: int
    carb_grams: int
    fiber_grams: int
    water_ounces: int


class HealthResponse(BaseModel):
    status: str
    provider: str | None  # "openai" | "groq"
    model: str | None
    prompt_version: str
    llm_key_present: bool
    fdc_key_present: bool
