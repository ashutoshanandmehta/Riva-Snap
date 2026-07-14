"""Accuracy eval: runs the scan pipeline over a golden image set.

Usage:
    cd scan-service
    .venv/bin/python eval/run_eval.py

Reads eval/golden.jsonl — one JSON object per line:
    {"file": "chicken_plate.jpg", "dish": "grilled chicken breast",
     "kcal": 450, "grams": 300, "scan_type": "food"}

Images live in eval/images/. Writes a markdown report to eval/reports/.
"""

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
    config = settings()
    if not GOLDEN_FILE.exists():
        sys.exit(f"No golden set at {GOLDEN_FILE} — see golden.example.jsonl.")

    golden = [json.loads(line) for line in GOLDEN_FILE.read_text().splitlines() if line.strip()]
    if not golden:
        sys.exit("golden.jsonl is empty.")

    try:
        client, provider = vision.make_client(config)
    except RuntimeError as error:
        sys.exit(str(error))
    model = vision.resolve_model(client, provider, config.riva_scan_model)
    prompt_text = vision.load_prompt(config.prompt_version)

    rows = []
    latencies: list[float] = []
    for case in golden:
        image_path = IMAGES_DIR / case["file"]
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
        rows.append(
            {
                **case,
                "detected": [item.name for item in result.items],
                "detected_kcal": result.totals.calories,
                "name_ok": name_matches(case["dish"], detected_names) if case.get("dish") else None,
                "type_ok": result.scan_type == case.get("scan_type", "food"),
                "fdc_matched": any(item.matched for item in result.items),
                "kcal_err": kcal_err,
                "latency_ms": round(elapsed_ms),
            }
        )

    scored = [r for r in rows if "error" not in r]
    name_rows = [r for r in scored if r["name_ok"] is not None]
    kcal_rows = [r for r in scored if r["kcal_err"] is not None]
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
        "| file | expected | detected | kcal (exp/got) | name | type | FDC | ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['file']} | {r.get('dish', '')} | ERROR: {r['error']} | | | | | |")
            continue
        lines.append(
            f"| {r['file']} | {r.get('dish', '')} | {', '.join(r['detected'])} "
            f"| {r.get('kcal', '—')}/{r['detected_kcal']} "
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
