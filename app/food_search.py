"""Food search: replacement candidates for one mis-detected scan item.

USDA prices, Claude only names and decomposes — the scan pipeline's principle
one level deeper. USDA knows "chicken breast" but not "Maggi"; it does know
wheat flour, palm oil and salt. So a dish FoodData Central cannot price is
handed to Claude for a recipe, and the *ingredients* are priced against USDA.
Only the proportions are the model's.

At most two text-only structured-output calls, mirroring `suggestions.py`:
one to propose alternative names when the user typed nothing, one to decompose
whatever USDA could not price. Both are best-effort — a model failure degrades
the candidate list, it never fails the request.
"""

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor

from . import grounding, plausibility, vision
from .config import Settings

logger = logging.getLogger("scan.food_search")

# Naming and decomposition are knowledge tasks, not reasoning ones.
DEFAULT_MODEL = "claude-sonnet-5"

# Suggestions shown in the editor's list. A typed search returns ONE result —
# the client applies it straight away rather than making the user choose.
MAX_SUGGESTIONS = 6
MAX_INGREDIENTS = 12
# Caps on everything that reaches a prompt or an FDC query. All of it is
# client- or model-supplied, and a food name is a few words.
MAX_NAME_CHARS = 120
MAX_CONTEXT_CHARS = 400
MAX_OTHER_ITEMS = 12
# Fallback portion when the client sends no grams (older build, or the scan
# never had a portion for this row).
DEFAULT_GRAMS = 100.0
# Backstop above every per-class bound in food_classes.json. The plausibility
# gate does the real clamping; this only stops a garbage number reaching it.
MAX_GRAMS = 5000.0

NUTRIENT_FIELDS = ("calories", "protein_g", "carb_g", "fiber_g", "fat_g", "sugar_g", "sodium_mg")

# Neither schema uses a `"type": [X, "null"]` union, so `vision._nullable_to_anyof`
# is not needed here (same choice as suggestions.SUGGEST_SCHEMA).
NAMES_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "portion_desc", "portion_grams"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Plain food name a US nutrition database would use.",
                    },
                    "portion_desc": {"type": "string"},
                    "portion_grams": {"type": "number"},
                },
            },
        },
    },
}

RECIPE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dishes"],
    "properties": {
        "dishes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "portion_desc", "portion_grams", "ingredients"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Echo the dish name exactly as it was given.",
                    },
                    "portion_desc": {"type": "string"},
                    "portion_grams": {"type": "number"},
                    "ingredients": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "grams", *NUTRIENT_FIELDS],
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": (
                                        "Single raw ingredient a US nutrition database"
                                        " would list, e.g. 'wheat flour', not 'noodle cake'."
                                    ),
                                },
                                "grams": {"type": "number"},
                                "calories": {"type": "number"},
                                "protein_g": {"type": "number"},
                                "carb_g": {"type": "number"},
                                "fiber_g": {"type": "number"},
                                "fat_g": {"type": "number"},
                                "sugar_g": {"type": "number"},
                                "sodium_mg": {"type": "number"},
                            },
                        },
                    },
                },
            },
        },
    },
}

_NAMES_PROMPT = (
    "You are the food-search engine of Riva, a US health app for people on GLP-1"
    " medication. A photo scan identified a food incorrectly and the user wants to"
    " swap it. Propose up to 5 foods they plausibly meant instead. Rules: name each"
    " one the way a US nutrition database would ('Grilled chicken breast', not"
    " 'yummy chicken'); stay consistent with the rest of the plate; vary the"
    " suggestions rather than offering five wordings of one food; never repeat the"
    " food that was detected; give each a realistic US portion in grams and a"
    " portion_desc a person would say out loud, with no dashes and no parentheses."
)

_RECIPE_PROMPT = (
    "You are the food-composition engine of Riva, a US health app for people on"
    " GLP-1 medication. The USDA FoodData Central database has no entry for these"
    " dishes, so break each one into the raw ingredients it is made from — those"
    " ingredients WILL be looked up in USDA, so name them the way that database"
    " would ('wheat flour', 'palm oil', 'table salt'). Rules: cover one stated"
    " portion of the dish, so the ingredient grams sum to roughly its"
    " portion_grams; include cooking fat, sauces and salt, which people forget;"
    f" at most {MAX_INGREDIENTS} ingredients, largest first; echo each dish name"
    " exactly as given. Also give every ingredient's nutrition FOR THE GRAMS YOU"
    " STATED (not per 100 g) — it is the fallback for ingredients USDA also lacks."
)


def resolve_model(config: Settings) -> str:
    """Explicit RIVA_FOOD_SEARCH_MODEL override, else the Sonnet default."""
    return config.riva_food_search_model or DEFAULT_MODEL


def _call(config: Settings, system: str, user_text: str, schema: dict) -> dict:
    """One text-only structured-output call. Raises on any failure."""
    client = vision.make_client(config)
    model = resolve_model(config)
    extra: dict = {} if "haiku" in model else {"thinking": {"type": "disabled"}}
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user_text}],
        **extra,
    )
    text = vision._anthropic_text(response)
    if not text:
        raise ValueError("Empty response from food-search model")
    return vision._parse(text)


# MARK: Naming


def propose_names(config: Settings, request) -> list[dict]:
    """Alternatives for a mis-detected item. Raises; the caller degrades."""
    context = {
        "detected_as": _capped(request.original_item),
        "plate": _capped(request.plate_context, MAX_CONTEXT_CHARS),
        "other_items_on_the_plate": [
            _capped(name) for name in request.other_items[:MAX_OTHER_ITEMS]
        ],
    }
    payload = _call(
        config,
        _NAMES_PROMPT,
        "The scan produced this. Suggest what the user may have meant instead:\n"
        + json.dumps(context, separators=(",", ":")),
        NAMES_SCHEMA,
    )
    raw = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise ValueError("Naming payload missing 'candidates' list")
    return [entry for entry in raw if isinstance(entry, dict) and entry.get("name")]


# MARK: Recipe decomposition


def decompose(config: Settings, seeds: list[dict]) -> dict[str, list[dict]]:
    """Ingredient lists for dishes USDA could not price, keyed by lowercased
    dish name. Raises; the caller degrades."""
    asked = [
        {
            "name": seed["name"],
            "portion_desc": seed["portion_desc"],
            "portion_grams": seed["grams"],
        }
        for seed in seeds
    ]
    payload = _call(
        config,
        _RECIPE_PROMPT,
        "Break these dishes into ingredients:\n" + json.dumps(asked, separators=(",", ":")),
        RECIPE_SCHEMA,
    )
    dishes = payload.get("dishes") if isinstance(payload, dict) else None
    if not isinstance(dishes, list):
        raise ValueError("Recipe payload missing 'dishes' list")

    recipes: dict[str, list[dict]] = {}
    for dish in dishes:
        if not isinstance(dish, dict):
            continue
        ingredients = [
            entry
            for entry in (dish.get("ingredients") or [])
            if isinstance(entry, dict) and entry.get("name")
        ]
        if ingredients:
            recipes[_capped(dish.get("name")).lower()] = ingredients[:MAX_INGREDIENTS]
    return recipes


# MARK: Pricing


def _price(fdc_api_key: str, names: list[str]) -> list:
    """USDA lookups in parallel — sequential ones dominate latency at ~1.5s each."""
    if not names:
        return []
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(
            pool.map(lambda name: grounding.best_match(fdc_api_key, _query(name))[0], names)
        )


def _zero() -> dict[str, float]:
    return dict.fromkeys(NUTRIENT_FIELDS, 0.0)


def _from_recipe(
    fdc_api_key: str, ingredients: list[dict], portion_grams: float
) -> dict[str, float] | None:
    """Sums a recipe, pricing each ingredient against USDA and falling back to
    the model's own numbers for the ones USDA also lacks.

    The recipe is treated as a statement of *proportions*, not of absolute
    mass: the sum is rescaled so it describes `portion_grams`. Without that,
    a model that lists 700 g of ingredients for a 70 g cake returns tenfold
    calories against a row still labelled 70 g.
    """
    matches = _price(fdc_api_key, [str(entry["name"]) for entry in ingredients])
    totals = _zero()
    recipe_grams = 0.0
    priced = 0
    for entry, candidate in zip(ingredients, matches):
        grams = max(float(entry.get("grams", 0) or 0), 0.0)
        if not math.isfinite(grams):
            continue
        recipe_grams += grams
        if candidate is not None and grams > 0:
            nutrients = grounding.grounded_nutrients(candidate, grams)
            priced += 1
        else:
            nutrients = {field: float(entry.get(field, 0) or 0) for field in NUTRIENT_FIELDS}
        for field in NUTRIENT_FIELDS:
            value = float(nutrients.get(field, 0.0))
            totals[field] += value if math.isfinite(value) else 0.0

    factor = portion_grams / recipe_grams if recipe_grams > 0 and portion_grams > 0 else 1.0
    if abs(factor - 1.0) > 0.01:
        logger.info(
            "recipe summed to %.0f g for a %.0f g portion; rescaling by %.2f",
            recipe_grams,
            portion_grams,
            factor,
        )
        totals = {field: value * factor for field, value in totals.items()}

    logger.info("recipe priced: %d/%d ingredients grounded", priced, len(ingredients))
    return totals if totals["calories"] > 0 else None


def _suggestion(name: str, portion_desc: str, grams: float, nutrients: dict, matched: bool) -> dict:
    """Applies the plausibility gate, then rounds to the units
    `main._assemble` uses for a scan item.

    The gate is the same one every scanned item passes through
    (`plausibility.adjust_item`): an edited item is logged by exactly the same
    path, so it has to clear exactly the same bar. Without it a client-supplied
    portion scales a per-100g value without limit.
    """
    gate = plausibility.gate_grams(grams, name)
    if gate.label != "ok":
        logger.info("food search portion gated: %s (%s)", gate.reason, name)
    factor = gate.factor if gate.factor > 0 and math.isfinite(gate.factor) else 1.0
    scaled = {field: nutrients.get(field, 0.0) * factor for field in NUTRIENT_FIELDS}
    return {
        "name": name,
        "portion_desc": portion_desc,
        "portion_grams": round(gate.grams, 1),
        "calories": round(scaled["calories"]),
        "protein_grams": round(scaled["protein_g"]),
        "carb_grams": round(scaled["carb_g"]),
        "fiber_grams": round(scaled["fiber_g"]),
        "fat_g": round(scaled["fat_g"], 1),
        "sugar_g": round(scaled["sugar_g"], 1),
        "sodium_mg": round(scaled["sodium_mg"], 1),
        "matched": matched,
    }


# MARK: Orchestration


def _capped(text, limit: int = MAX_NAME_CHARS) -> str:
    """Trims untrusted text to something prompt-sized."""
    return str(text or "").strip()[:limit]


def _query(name: str) -> str:
    """A food name, made safe for an FDC query.

    FoodData Central answers HTTP 400 for a parenthetical query, and
    `fdc.search_foods` cannot tell that apart from a quota failure — it logs a
    warning and returns no candidates, so the food silently falls through to
    the recipe path. Stripping the brackets keeps the words and gets an answer.
    """
    return _capped(name).replace("(", " ").replace(")", " ").strip()


def _portion(value) -> float:
    """A portion grams figure, made safe before it scales a per-100g value.

    Both sources are untrusted: `original_grams` comes straight off the wire,
    and `portion_grams` off the model. NaN, infinity and negatives become the
    default; the cap is a backstop below which the plausibility gate in
    `_suggestion` does the real, per-food-class clamping.
    """
    try:
        grams = float(value or 0)
    except (TypeError, ValueError):
        return DEFAULT_GRAMS
    if not math.isfinite(grams) or grams <= 0:
        return DEFAULT_GRAMS
    return min(grams, MAX_GRAMS)


def _seeds(config: Settings, request) -> list[dict]:
    """The foods to price: the typed one, or model-proposed alternatives."""
    query = _capped(request.search)
    if query:
        # The user is swapping one food for another in the same spot on the
        # plate, so the portion the scan measured carries over.
        return [
            {
                "name": query,
                "portion_desc": _capped(request.original_portion_desc) or "1 serving",
                "grams": _portion(request.original_grams),
            }
        ]

    try:
        proposed = propose_names(config, request)
    except Exception:
        # Degraded but useful: USDA's own near-misses for the detected name are
        # alternatives by definition. Returned directly rather than through the
        # loop below, whose whole job is to exclude that very name.
        logger.exception("name suggestion failed; falling back to the detected item")
        return [
            {
                "name": _capped(request.original_item),
                "portion_desc": _capped(request.original_portion_desc) or "1 serving",
                "grams": _portion(request.original_grams),
            }
        ]

    seeds: list[dict] = []
    seen = {_capped(request.original_item).lower()}
    for entry in proposed:
        name = _capped(entry.get("name"))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        seeds.append(
            {
                "name": name,
                "portion_desc": _capped(entry.get("portion_desc")) or "1 serving",
                "grams": _portion(entry.get("portion_grams")),
            }
        )
        if len(seeds) == MAX_SUGGESTIONS:
            break
    return seeds


def search(config: Settings, request) -> list[dict]:
    """Priced replacement candidates for one scan item.

    A typed search returns at most one result (the client applies it straight
    away); an empty search returns up to `MAX_SUGGESTIONS` for the picker.
    """
    seeds = _seeds(config, request)
    if not seeds:
        return []

    try:
        matches = _price(config.fdc_api_key, [seed["name"] for seed in seeds])
    except Exception:
        # `fdc.search_foods` swallows HTTP errors but not a malformed body.
        # Everything here is still recoverable through the recipe path.
        logger.exception("USDA lookup failed; composing every candidate instead")
        matches = [None] * len(seeds)

    results: list[dict] = []
    unmatched: list[dict] = []
    for seed, candidate in zip(seeds, matches):
        if candidate is None:
            unmatched.append(seed)
            continue
        results.append(
            _suggestion(
                seed["name"],
                seed["portion_desc"],
                seed["grams"],
                # A candidate may lack a nutrient FDC never measured; treat it as 0
                # rather than dropping an otherwise good match.
                {**_zero(), **grounding.grounded_nutrients(candidate, seed["grams"])},
                matched=True,
            )
        )

    if unmatched:
        results.extend(_recipe_results(config, unmatched))

    logger.info(
        "food search %r: %d seeds -> %d results (%d USDA, %d composed)",
        request.search.strip() or request.original_item,
        len(seeds),
        len(results),
        sum(1 for r in results if r["matched"]),
        sum(1 for r in results if not r["matched"]),
    )
    return results[:MAX_SUGGESTIONS]


def _recipe_results(config: Settings, unmatched: list[dict]) -> list[dict]:
    """USDA had nothing for these, so compose them from a Claude recipe.

    Best-effort throughout: a model failure drops these candidates rather than
    failing a request that may already carry good USDA matches.
    """
    composed: list[dict] = []
    try:
        recipes = decompose(config, unmatched)
        for seed in unmatched:
            ingredients = recipes.get(seed["name"].strip().lower())
            if not ingredients:
                continue
            nutrients = _from_recipe(config.fdc_api_key, ingredients, seed["grams"])
            if nutrients is None:
                continue
            composed.append(
                # matched stays False: the ingredients may be USDA-priced, but
                # their proportions are the model's, so this is an estimate.
                _suggestion(
                    seed["name"], seed["portion_desc"], seed["grams"], nutrients, matched=False
                )
            )
    except Exception:
        # Covers the ingredient pricing too, not just the model call — a
        # malformed FDC body must not take down a request that may already
        # carry good USDA matches.
        logger.exception("recipe composition failed; dropping %d candidate(s)", len(unmatched))
        return composed
    return composed
