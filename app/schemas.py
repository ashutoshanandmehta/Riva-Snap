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
    # Plausibility gate verdict: "ok" | "clamped" (grams clamped to a per-class
    # bound, macros scaled, confidence lowered) | "implausible" (grossly out of
    # range — clamped but flag for the user to correct/retake). Never raw-out-of-range.
    plausibility: str = "ok"


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


class VolumetricDebug(BaseModel):
    """Verdict/diagnostic block for the multi-frame volumetric pipeline
    (`POST /v1/scan/volumetric`), behind the iOS "3D scan (beta)" flow.
    Absent (None) on the plain `/v1/scan` route.

    These fields are for eval/diagnostics, not end-user display — the iOS
    client does not decode this block for the shipping UI and must never
    render any of it as raw text; it shows only a simple, static
    "experimental measurement" notice instead."""

    tier: str  # "A" | "B" | "C"
    n_views: int
    segmenter: str
    parametric_fallback: bool  # True for B1 (poses not yet used)
    poses_present: bool
    food_class: str | None = None
    volume_ml: float | None = None  # gated volume
    raw_volume_ml: float | None = None
    mass_g: float | None = None
    gate_action: str | None = None  # "log" | "clamp" | "retake"
    mass_source: str | None = None  # "carve" | "llm" — which mass estimate was logged
    reason: str | None = None


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
    volumetric: VolumetricDebug | None = None


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


class DeviceSessionRequest(BaseModel):
    """A stable client-generated id (UUID) identifying one device."""

    device_id: str


class DeviceSession(BaseModel):
    """Session for a silently provisioned per-device account."""

    access_token: str
    refresh_token: str | None
    expires_at: int | None
    user_id: str
    email: str


INJECTION_SITES = (
    "right_arm",
    "left_arm",
    "lower_left_abs",
    "lower_right_abs",
    "right_thigh",
    "left_thigh",
)

SIDE_EFFECTS = (
    "nausea",
    "headache",
    "fatigue",
    "constipation",
    "diarrhea",
    "dizziness",
    "bloating",
    "heartburn",
    "food_noise",
)


class WeightLogRequest(BaseModel):
    pounds: float
    measured_at: str | None = None


class WeightLogResult(BaseModel):
    weight_id: str
    pounds: float
    dose_mg: float | None
    measured_at: str


class ShotLogRequest(BaseModel):
    medication_name: str
    dose_mg: float
    injection_site: str
    comfort_rating: int | None = None
    taken_at: str | None = None


class ShotLogResult(BaseModel):
    shot_id: str
    medication_name: str
    dose_mg: float
    taken_at: str
    injection_site: str


class SideEffectItem(BaseModel):
    effect: str
    severity: int


class SideEffectsLogRequest(BaseModel):
    effects: list[SideEffectItem]
    note: str | None = None


class SideEffectsLogResult(BaseModel):
    log_date: str | None
    effects: list[SideEffectItem]


class CheckinLogRequest(BaseModel):
    question_id: str
    option_code: str


class CheckinLogResult(BaseModel):
    checkin_date: str
    question_id: str
    option_code: str
    label: str
    value: int


WELLNESS_KINDS = ("yoga", "meditation", "exercise", "mind", "sleep")


class WellnessLogRequest(BaseModel):
    practice_id: str
    kind: str
    minutes: int


class WellnessLogResult(BaseModel):
    day: str
    minutes_today: int
    streak_days: int


class WellnessSuggestion(BaseModel):
    practice_id: str
    reason: str


class WellnessSuggestionsResult(BaseModel):
    suggestions: list[WellnessSuggestion]
    source: str  # "llm" | "cache" | "fallback"


TODO_CATEGORIES = ("food", "water", "weight", "custom")

TODO_REPEATS = ("daily", "once")


class Todo(BaseModel):
    """One to-do as `list_todos` returns it. `is_done` is resolved server-side
    against the profile-timezone day, so daily to-dos reset on their own."""

    id: str
    title: str
    category: str
    repeat_rule: str
    remind_hour: int
    remind_minute: int
    due_date: str | None
    is_done: bool


class TodoUpsertRequest(BaseModel):
    """Create when `id` is absent, edit the owned to-do when it is present."""

    id: str | None = None
    title: str
    category: str
    repeat_rule: str
    remind_hour: int
    remind_minute: int
    due_date: str | None = None


class TodoDoneRequest(BaseModel):
    done: bool


class TodoListResult(BaseModel):
    todos: list[Todo]


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
    provider: str | None  # "anthropic"
    model: str | None
    prompt_version: str
    llm_key_present: bool
    fdc_key_present: bool


GENDERS = ("female", "male", "non-binary", "prefer-not-to-say")


class Profile(BaseModel):
    name: str
    date_of_birth: str | None
    gender: str | None
    clinician_name: str | None
    start_weight: float | None
    goal_weight: float | None
    height_inches: float | None
    timezone: str


class NutritionGoals(BaseModel):
    protein_goal: int
    carb_goal: int
    fiber_goal: int
    water_goal: int
    # Optional so rows from a pre-0003 database still validate.
    wellness_minutes_goal: int | None = None


class HealthGoals(BaseModel):
    glp1_support: bool
    weight_mgmt: bool
    nutrition_diet: bool
    muscle_preserve: bool
    exercise_move: bool
    sleep_recovery: bool


class MedicationPlan(BaseModel):
    name: str
    current_dose_mg: float
    cadence_days: int
    dose_frequency: str
    reminder_description: str | None
    start_date: str | None


class MeResponse(BaseModel):
    """Everything the Profile tab renders in one call."""

    profile: Profile
    nutrition_goals: NutritionGoals
    health_goals: HealthGoals
    plan: MedicationPlan | None


class ProfileUpdateRequest(BaseModel):
    """Any subset of the profile fields; omitted fields keep their value."""

    name: str | None = None
    date_of_birth: str | None = None
    gender: str | None = None
    clinician_name: str | None = None
    start_weight: float | None = None
    goal_weight: float | None = None
    height_inches: float | None = None
    timezone: str | None = None


class ProfileUpdateResult(BaseModel):
    profile: Profile


class GoalsUpdateRequest(BaseModel):
    protein_goal: int | None = None
    carb_goal: int | None = None
    fiber_goal: int | None = None
    water_goal: int | None = None
    wellness_minutes_goal: int | None = None


class GoalsUpdateResult(BaseModel):
    nutrition_goals: NutritionGoals


class HealthGoalsUpdateRequest(BaseModel):
    glp1_support: bool | None = None
    weight_mgmt: bool | None = None
    nutrition_diet: bool | None = None
    muscle_preserve: bool | None = None
    exercise_move: bool | None = None
    sleep_recovery: bool | None = None


class HealthGoalsUpdateResult(BaseModel):
    health_goals: HealthGoals


class PlanUpdateRequest(BaseModel):
    name: str | None = None
    current_dose_mg: float | None = None
    cadence_days: int | None = None
    reminder_description: str | None = None


class PlanUpdateResult(BaseModel):
    plan: MedicationPlan


class WeightEntry(BaseModel):
    id: str
    pounds: float
    dose_mg: float | None
    measured_at: str


class WeightListResult(BaseModel):
    entries: list[WeightEntry]


class ShotEntry(BaseModel):
    id: str
    medication_name: str
    dose_mg: float
    taken_at: str
    injection_site: str
    comfort_rating: int | None


class ShotListResult(BaseModel):
    entries: list[ShotEntry]


class SideEffectDayLog(BaseModel):
    log_date: str
    note: str | None
    effects: list[SideEffectItem]


class SideEffectListResult(BaseModel):
    logs: list[SideEffectDayLog]


class AccountDeleteResult(BaseModel):
    deleted: bool
