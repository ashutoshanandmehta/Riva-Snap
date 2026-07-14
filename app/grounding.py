"""Grounding: match vision items to USDA foods and recompute nutrients.

Matched items get canonical per-100g USDA values scaled by the estimated
portion (consistent, defensible numbers — the MATCHED badge). Unmatched
items keep the model's estimate.
"""

import re

from . import fdc

# Minimum token-coverage score to accept a USDA match. Tuning surface —
# raise for precision, lower for coverage; the eval harness measures both.
MATCH_THRESHOLD = 0.6

# Descriptions containing these form tokens are penalized unless the item
# name itself mentions them — prevents "white rice" (a cooked dish) matching
# "Flour, rice, white" whose per-100g values are wildly different from the
# plated food. NOTE: "raw" is deliberately NOT penalized — it is the correct
# USDA form for fresh produce ("Oranges, raw").
FORM_PENALTY_TOKENS = {
    "flour", "dry", "dried", "dehydrated", "powder", "mix",
    "concentrate", "unprepared", "babyfood",
}
FORM_PENALTY = 0.35

# Bonus when the description's LEADING word matches an item token. FDC leads
# with the food category ("Oranges, raw…" vs "Sherbet, orange") — this ranks
# the actual food above products that merely contain/flavor it.
FIRST_TOKEN_BONUS = 0.25

_STOPWORDS = {"a", "an", "the", "of", "with", "and", "or", "in", "on", "style"}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return {w.rstrip("s") for w in words if w not in _STOPWORDS and len(w) > 2}


def match_score(item_name: str, description: str) -> float:
    """Scores an FDC candidate for an item name.

    coverage of item tokens (primary) + leading-category bonus - wrong-form
    penalty. All three are tuning surfaces measured by the eval harness.
    """
    item_tokens = _tokens(item_name)
    if not item_tokens:
        return 0.0
    description_tokens = _tokens(description)
    score = len(item_tokens & description_tokens) / len(item_tokens)

    first_word = re.findall(r"[a-z]+", description.lower())
    if first_word and first_word[0].rstrip("s") in item_tokens:
        score += FIRST_TOKEN_BONUS

    if (description_tokens & FORM_PENALTY_TOKENS) - item_tokens:
        score -= FORM_PENALTY
    return score


def best_match(api_key: str, item_name: str) -> tuple[fdc.FdcCandidate | None, list[dict]]:
    """Returns (best candidate or None, debug info about the query)."""
    candidates = fdc.search_foods(api_key, item_name)

    scored: list[tuple[float, int, fdc.FdcCandidate]] = []
    for candidate in candidates:
        score = match_score(item_name, candidate["description"])
        if score < MATCH_THRESHOLD:
            continue
        # Prefer more-verified data types on ties (lower rank wins).
        try:
            rank = fdc.DATA_TYPE_PREFERENCE.index(candidate["data_type"])
        except ValueError:
            rank = len(fdc.DATA_TYPE_PREFERENCE)
        scored.append((score, rank, candidate))

    debug = {
        "query": item_name,
        "candidates": [
            {
                "fdc_id": c["fdc_id"],
                "description": c["description"],
                "data_type": c["data_type"],
                "score": round(match_score(item_name, c["description"]), 3),
            }
            for c in candidates
        ],
    }

    if not scored:
        return None, [debug]

    scored.sort(key=lambda entry: (-entry[0], entry[1], len(entry[2]["description"])))
    return scored[0][2], [debug]


def grounded_nutrients(candidate: fdc.FdcCandidate, grams: float) -> dict[str, float]:
    """Scales the candidate's per-100g values to the estimated portion."""
    factor = max(grams, 0.0) / 100.0
    return {
        field: round(value * factor, 1)
        for field, value in candidate["nutrients"].items()
    }
