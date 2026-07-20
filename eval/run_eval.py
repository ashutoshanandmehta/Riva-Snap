"""Accuracy eval: runs the scan pipeline over a golden image set.

Usage:
    cd scan-service
    .venv/bin/python eval/run_eval.py

Reads eval/golden.jsonl — one JSON object per line:
    {"file": "chicken_plate.jpg", "dish": "grilled chicken breast",
     "kcal": 450, "grams": 300, "scan_type": "food"}

Images live in eval/images/. Writes a markdown report to eval/reports/.
"""

import argparse
import difflib
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import grounding, preprocess, vision  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import _assemble  # noqa: E402

IMAGES_DIR = ROOT / "eval" / "images"
GOLDEN_FILE = ROOT / "eval" / "golden.jsonl"
REPORTS_DIR = ROOT / "eval" / "reports"

NAME_MATCH_THRESHOLD = 0.6


def name_matches(expected: str, item_names: list[str]) -> bool:
    """Fuzzy top-1-or-alternative dish-name match."""
    expected_norm = expected.lower().strip()
    for candidate in item_names:
        ratio = difflib.SequenceMatcher(None, expected_norm, candidate.lower()).ratio()
        token_score = grounding.match_score(expected, candidate)
        if ratio >= NAME_MATCH_THRESHOLD or token_score >= NAME_MATCH_THRESHOLD:
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the scan pipeline over a golden image set.")
    ap.add_argument("--golden", type=Path, default=GOLDEN_FILE, help="path to golden jsonl")
    ap.add_argument("--images", type=Path, default=IMAGES_DIR, help="directory of eval images")
    ap.add_argument("--limit", type=int, default=None, help="cap the number of cases (cost control)")
    args = ap.parse_args()
    golden_file, images_dir = args.golden, args.images

    config = settings()
    if not golden_file.exists():
        sys.exit(f"No golden set at {golden_file} — see golden.example.jsonl.")

    golden = [json.loads(line) for line in golden_file.read_text().splitlines() if line.strip()]
    if not golden:
        sys.exit(f"{golden_file} is empty.")
    if args.limit:
        golden = golden[: args.limit]

    try:
        client, provider = vision.make_client(config)
    except RuntimeError as error:
        sys.exit(str(error))
    model = vision.resolve_model(client, provider, config.riva_scan_model)
    prompt_text = vision.load_prompt(config.prompt_version)

    rows = []
    latencies: list[float] = []
    for case in golden:
        image_path = images_dir / case["file"]
        if not image_path.exists():
            rows.append({**case, "error": "image missing"})
            continue

        started = time.monotonic()
        try:
            image_b64 = preprocess.prepare_image(image_path.read_bytes())
            analysis = vision.analyze_image(
                client, model, image_b64, None, prompt_text, provider=provider
            )
            result = _assemble(analysis, config.fdc_api_key)
        except Exception as error:  # keep evaluating the rest of the set
            rows.append({**case, "error": str(error)})
            continue
        elapsed_ms = (time.monotonic() - started) * 1000
        latencies.append(elapsed_ms)

        detected_names: list[str] = []
        for item in result.items:
            detected_names.append(item.name)
            detected_names.extend(item.alternatives)

        expected_kcal = case.get("kcal")
        kcal_err = (
            abs(result.totals.calories - expected_kcal) / expected_kcal
            if expected_kcal
            else None
        )

        # Portion accuracy: total detected grams vs ground-truth mass. This is
        # the metric Nutrition5k is uniquely good for and the one the harness
        # previously ignored.
        detected_grams = sum(item.portion_grams for item in result.items)
        expected_grams = case.get("grams")
        gram_err = (
            abs(detected_grams - expected_grams) / expected_grams
            if expected_grams
            else None
        )

        # Ingredient recall: fraction of the ground-truth ingredient list the
        # model surfaced (as a detected item name or alternative).
        expected_ingrs = case.get("ingredients") or []
        ingr_recall = (
            sum(name_matches(ingr, detected_names) for ingr in expected_ingrs) / len(expected_ingrs)
            if expected_ingrs
            else None
        )

        rows.append(
            {
                **case,
                "detected": [item.name for item in result.items],
                "detected_kcal": result.totals.calories,
                "detected_grams": round(detected_grams),
                "name_ok": name_matches(case["dish"], detected_names) if case.get("dish") else None,
                "type_ok": result.scan_type == case.get("scan_type", "food"),
                "fdc_matched": any(item.matched for item in result.items),
                "kcal_err": kcal_err,
                "gram_err": gram_err,
                "ingr_recall": ingr_recall,
                "latency_ms": round(elapsed_ms),
            }
        )

    scored = [r for r in rows if "error" not in r]
    name_rows = [r for r in scored if r["name_ok"] is not None]
    kcal_rows = [r for r in scored if r["kcal_err"] is not None]
    gram_rows = [r for r in scored if r.get("gram_err") is not None]
    ingr_rows = [r for r in scored if r.get("ingr_recall") is not None]
    food_rows = [r for r in scored if r.get("scan_type", "food") == "food"]

    def pct(part: int, whole: int) -> str:
        return f"{100 * part / whole:.0f}%" if whole else "n/a"

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{timestamp}.md"

    lines = [
        f"# Scan eval — {timestamp}",
        "",
        f"- Model: `{model}` · Prompt: `{config.prompt_version}` · Cases: {len(golden)} "
        f"(errors: {len(rows) - len(scored)})",
        "",
        "## Metrics",
        "",
        f"| Dish-name match | {pct(sum(bool(r['name_ok']) for r in name_rows), len(name_rows))} |",
        "|---|---|",
        f"| Calorie MAPE | "
        + (f"{100 * statistics.mean(r['kcal_err'] for r in kcal_rows):.0f}%" if kcal_rows else "n/a")
        + " |",
        f"| Portion (gram) MAPE | "
        + (f"{100 * statistics.mean(r['gram_err'] for r in gram_rows):.0f}%" if gram_rows else "n/a")
        + " |",
        f"| Ingredient recall | "
        + (f"{100 * statistics.mean(r['ingr_recall'] for r in ingr_rows):.0f}%" if ingr_rows else "n/a")
        + " |",
        f"| scan_type accuracy | {pct(sum(r['type_ok'] for r in scored), len(scored))} |",
        f"| FDC match rate (food) | {pct(sum(r['fdc_matched'] for r in food_rows), len(food_rows))} |",
        f"| Latency p50 / p95 | "
        + (
            f"{statistics.median(latencies):.0f} ms / "
            f"{sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]:.0f} ms"
            if latencies
            else "n/a"
        )
        + " |",
        "",
        "## Cases",
        "",
        "| file | expected | detected | kcal (exp/got) | grams (exp/got) | name | type | FDC | ms |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['file']} | {r.get('dish', '')} | ERROR: {r['error']} | | | | | | |")
            continue
        lines.append(
            f"| {r['file']} | {r.get('dish', '')} | {', '.join(r['detected'])} "
            f"| {r.get('kcal', '—')}/{r['detected_kcal']} "
            f"| {r.get('grams', '—')}/{r['detected_grams']} "
            f"| {'✅' if r['name_ok'] else '❌' if r['name_ok'] is not None else '—'} "
            f"| {'✅' if r['type_ok'] else '❌'} "
            f"| {'✅' if r['fdc_matched'] else '—'} "
            f"| {r['latency_ms']} |"
        )

    report_path.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    main()
