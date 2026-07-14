"""USDA FoodData Central client: search + per-100g nutrient extraction."""

import logging

import httpx

logger = logging.getLogger("scan.fdc")

SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"

# Shared client: connection pooling across the parallel per-item lookups.
_http = httpx.Client(timeout=8.0)

# Generic (non-branded) data types, most-lab-verified first. FNDDS survey
# foods cover composed American dishes ("burrito with chicken and rice").
DATA_TYPE_PREFERENCE = ["Foundation", "SR Legacy", "Survey (FNDDS)"]

# FDC nutrient ids → our field names (values are per 100 g).
NUTRIENT_IDS = {
    1008: "calories",     # Energy (kcal)
    1003: "protein_g",
    1005: "carb_g",
    1079: "fiber_g",
    1004: "fat_g",
    2000: "sugar_g",      # Sugars, total
    1093: "sodium_mg",
}


class FdcCandidate(dict):
    """Search hit: fdc_id, description, data_type, nutrients (per 100 g)."""


def search_foods(api_key: str, query: str, page_size: int = 6) -> list[FdcCandidate]:
    """Returns candidate generic foods for a dish-name query."""
    try:
        response = _http.get(
            SEARCH_URL,
            params={
                "api_key": api_key,
                "query": query,
                "dataType": ",".join(DATA_TYPE_PREFERENCE),
                "pageSize": page_size,
            },
            timeout=8.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        # Grounding is best-effort: a network/quota failure must never fail
        # the scan — the model estimate is the fallback.
        logger.warning("FDC search failed for %r: %s", query, error)
        return []

    candidates: list[FdcCandidate] = []
    for food in response.json().get("foods", []):
        nutrients: dict[str, float] = {}
        for entry in food.get("foodNutrients", []):
            field = NUTRIENT_IDS.get(entry.get("nutrientId"))
            if field is not None and entry.get("value") is not None:
                nutrients[field] = float(entry["value"])
        if "calories" not in nutrients:
            continue  # useless for grounding without energy
        candidates.append(
            FdcCandidate(
                fdc_id=food["fdcId"],
                description=food.get("description", ""),
                data_type=food.get("dataType", ""),
                nutrients=nutrients,
            )
        )
    return candidates
