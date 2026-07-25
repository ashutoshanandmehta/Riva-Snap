"""Plausibility gate for scan items.

Validate each item's portion grams against a per-food-class plausible mass range
BEFORE anything is logged. Out-of-range grams are clamped to the bound and flagged
(never silently logged); the item's macros scale with the clamp so it stays
internally consistent. Mass bounds are derived from the per-class volume x density
in food_classes.json; unknown foods fall back to `_generic`.

This is the CPU-side gate that applies today to the V1 LLM estimates. The volumetric
pipeline reuses the same table + class resolution for its own volume gate.
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
RETAKE_FACTOR = 3.0  # beyond this multiple of a bound => "implausible" (flag hard)


@lru_cache
def food_classes() -> dict:
    return json.loads((HERE / "food_classes.json").read_text())


def resolve_class(name: str | None, llm_class: str | None = None) -> tuple[str, dict]:
    """Detected food name -> (class_key, record). A valid `llm_class` (a real,
    non-internal key that isn't the "other" catch-all) wins outright without
    the alias scan; otherwise falls back to longest-alias-wins, else _generic."""
    classes = food_classes()
    if llm_class and llm_class != "other" and not llm_class.startswith("_"):
        rec = classes.get(llm_class)
        if rec is not None:
            return llm_class, rec
    if not name:
        return "_generic", classes["_generic"]
    n = name.lower()
    best_key, best_len = None, 0
    for key, rec in classes.items():
        if key.startswith("_"):
            continue
        for alias in rec.get("aliases", []):
            if alias in n and len(alias) > best_len:
                best_key, best_len = key, len(alias)
    return (best_key, classes[best_key]) if best_key else ("_generic", classes["_generic"])


def _mass_bounds(rec: dict) -> tuple[float, float]:
    vmin, vmax = rec["volume_ml"]
    dmin, _, dmax = rec["density_g_ml"]
    return vmin * dmin, vmax * dmax


@dataclass
class GateGrams:
    food_class: str
    grams: float  # possibly clamped
    factor: float  # clamped/original (1.0 = unchanged)
    label: str  # "ok" | "clamped" | "implausible"
    reason: str


def gate_grams(grams: float, name: str | None) -> GateGrams:
    key, rec = resolve_class(name)
    lo, hi = _mass_bounds(rec)
    if grams is None or grams <= 0:
        return GateGrams(key, grams, 1.0, "ok", "non-positive grams left as-is")
    if grams < lo:
        label = "implausible" if grams < lo / RETAKE_FACTOR else "clamped"
        return GateGrams(
            key,
            round(lo, 1),
            lo / grams,
            label,
            f"{grams:.0f} g below class min {lo:.0f} g — clamped up",
        )
    if grams > hi:
        label = "implausible" if grams > hi * RETAKE_FACTOR else "clamped"
        return GateGrams(
            key,
            round(hi, 1),
            hi / grams,
            label,
            f"{grams:.0f} g above class max {hi:.0f} g — clamped down",
        )
    return GateGrams(key, grams, 1.0, "ok", "in plausible range")


def adjust_item(item) -> None:
    """Apply the gate to a ScanItem in place, scaling macros by the clamp factor and
    lowering confidence when clamped. Sets item.plausibility to ok/clamped/implausible."""
    res = gate_grams(item.portion_grams, item.name)
    item.plausibility = res.label
    if res.factor == 1.0:
        return
    f = res.factor
    item.portion_grams = round(res.grams, 1)
    item.calories = round(item.calories * f)
    item.protein_grams = round(item.protein_grams * f)
    item.carb_grams = round(item.carb_grams * f)
    item.fiber_grams = round(item.fiber_grams * f)
    item.extended.fat_g = round(item.extended.fat_g * f, 1)
    item.extended.sugar_g = round(item.extended.sugar_g * f, 1)
    item.extended.sodium_mg = round(item.extended.sodium_mg * f, 1)
    item.confidence = "low"
