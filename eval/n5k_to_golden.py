"""Build a golden eval set from the Nutrition5k dataset.

Nutrition5k (Google Research) gives ground-truth total mass, calories, and
macros per plated dish, plus a per-ingredient breakdown. We convert a sample
into the same golden.jsonl format eval/run_eval.py reads, so we can measure
where the scan pipeline loses accuracy: dish/ingredient identification, portion
(grams), and calories.

Caveat worth remembering: Nutrition5k images are top-down shots from a fixed lab
rig with depth sensors. They do not look like a user's angled phone photo, so
treat the numbers as a stress test of portion/calorie estimation on ground
truth, not as a measure of real-world app accuracy.

Downloads over plain HTTPS (the bucket is public), so no gcloud/gsutil needed.
Stdlib only.

Usage (from backend/):
    .venv/bin/python eval/n5k_to_golden.py --n 50
    .venv/bin/python eval/n5k_to_golden.py --n 100 --cafe both --seed 7

Then:
    .venv/bin/python eval/run_eval.py --golden eval/golden.n5k.jsonl
"""

import argparse
import json
import random
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGES_DIR = ROOT / "eval" / "images"
CACHE_DIR = ROOT / "eval" / "n5k_cache"
DEFAULT_OUT = ROOT / "eval" / "golden.n5k.jsonl"

BASE = "https://storage.googleapis.com/nutrition5k_dataset/nutrition5k_dataset"
META = {
    "cafe1": f"{BASE}/metadata/dish_metadata_cafe1.csv",
    "cafe2": f"{BASE}/metadata/dish_metadata_cafe2.csv",
}
SPLIT = {
    "train": f"{BASE}/dish_ids/splits/rgb_train_ids.txt",
    "test": f"{BASE}/dish_ids/splits/rgb_test_ids.txt",
}
IMAGE_URL = f"{BASE}/imagery/realsense_overhead/{{dish_id}}/rgb.png"

# dish-level prefix: dish_id + 5 totals, then ingredients repeat in 7-field groups
DISH_PREFIX = 6
INGR_FIELDS = 7

socket.setdefaulttimeout(60)


def fetch_text(url: str, cache_name: str) -> str:
    """Download a text file, caching it under eval/n5k_cache/."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / cache_name
    if cached.exists() and cached.stat().st_size > 0:
        return cached.read_text()
    print(f"  downloading {cache_name} …", flush=True)
    text = urllib.request.urlopen(url).read().decode("utf-8", "replace")
    cached.write_text(text)
    return text


def fetch_image(dish_id: str, dest: Path) -> bool:
    """Download one overhead rgb.png. Returns False if it is missing."""
    if dest.exists() and dest.stat().st_size > 0:
        return True
    try:
        with urllib.request.urlopen(IMAGE_URL.format(dish_id=dish_id)) as resp:
            data = resp.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise
    dest.write_bytes(data)
    return True


def parse_dish(line: str) -> dict | None:
    """Parse one ragged metadata row into a dish record, or None if malformed."""
    fields = line.split(",")
    if len(fields) < DISH_PREFIX:
        return None
    dish_id = fields[0]
    try:
        total_cal, total_mass, total_fat, total_carb, total_protein = (
            float(x) for x in fields[1:DISH_PREFIX]
        )
    except ValueError:
        return None

    ingredients = []
    rest = fields[DISH_PREFIX:]
    for i in range(0, len(rest) - INGR_FIELDS + 1, INGR_FIELDS):
        name = rest[i + 1].strip()
        try:
            grams = float(rest[i + 2])
        except ValueError:
            continue
        if name:
            ingredients.append({"name": name, "grams": grams})
    return {
        "dish_id": dish_id,
        "calories": total_cal,
        "mass": total_mass,
        "fat": total_fat,
        "carb": total_carb,
        "protein": total_protein,
        "ingredients": ingredients,
    }


def to_golden(dish: dict) -> dict:
    """Convert a dish record into a golden.jsonl case."""
    # Dominant ingredient is the best single-name label for a composite plate;
    # the full ingredient list drives the recall metric in run_eval.py.
    ingr_names = [g["name"] for g in dish["ingredients"]]
    dominant = ""
    if dish["ingredients"]:
        dominant = max(dish["ingredients"], key=lambda g: g["grams"])["name"]
    return {
        "file": f"n5k_{dish['dish_id']}.png",
        "dish": dominant,
        "ingredients": ingr_names,
        "kcal": round(dish["calories"]),
        "grams": round(dish["mass"]),
        "protein": round(dish["protein"]),
        "carb": round(dish["carb"]),
        "fat": round(dish["fat"]),
        "scan_type": "food",
        "source": "nutrition5k",
        "dish_id": dish["dish_id"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=50, help="number of dishes to sample")
    ap.add_argument("--split", choices=["test", "train"], default="test",
                    help="which rgb split to sample from (default: held-out test)")
    ap.add_argument("--cafe", choices=["cafe1", "cafe2", "both"], default="both")
    ap.add_argument("--seed", type=int, default=7, help="sampling seed (reproducible)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--min-mass", type=float, default=10.0,
                    help="skip near-empty dishes below this gram mass")
    args = ap.parse_args()

    print("Loading Nutrition5k metadata …")
    split_ids = {
        line.strip() for line in fetch_text(SPLIT[args.split], f"rgb_{args.split}_ids.txt").splitlines()
        if line.strip()
    }
    print(f"  {len(split_ids)} dish ids in {args.split} split")

    cafes = ["cafe1", "cafe2"] if args.cafe == "both" else [args.cafe]
    dishes: dict[str, dict] = {}
    for cafe in cafes:
        for line in fetch_text(META[cafe], f"dish_metadata_{cafe}.csv").splitlines():
            if not line.strip():
                continue
            dish = parse_dish(line)
            if dish and dish["dish_id"] in split_ids and dish["mass"] >= args.min_mass:
                dishes[dish["dish_id"]] = dish
    print(f"  {len(dishes)} dishes eligible (in split, mass >= {args.min_mass}g)")

    if not dishes:
        sys.exit("No eligible dishes — check the split/cafe filters.")

    order = sorted(dishes)  # deterministic base order before seeded shuffle
    random.Random(args.seed).shuffle(order)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading up to {args.n} overhead images into eval/images/ …")
    cases = []
    missing = 0
    for dish_id in order:
        if len(cases) >= args.n:
            break
        dest = IMAGES_DIR / f"n5k_{dish_id}.png"
        if fetch_image(dish_id, dest):
            cases.append(to_golden(dishes[dish_id]))
            if len(cases) % 10 == 0:
                print(f"  {len(cases)}/{args.n} …", flush=True)
        else:
            missing += 1

    args.out.write_text("\n".join(json.dumps(c) for c in cases) + "\n")
    print(f"\nWrote {len(cases)} cases to {args.out.relative_to(ROOT)}")
    if missing:
        print(f"({missing} dishes skipped: no overhead image)")
    if len(cases) < args.n:
        print(f"NOTE: only {len(cases)} of the requested {args.n} had images available.")
    print(f"\nRun the eval with:\n  .venv/bin/python eval/run_eval.py --golden {args.out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
