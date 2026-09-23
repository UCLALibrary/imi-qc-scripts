"""Focus QC for IMI manuscript images served over IIIF. See README.md."""
import argparse
import csv
import hashlib
import io
import re
import shutil
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import laplace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path.cwd()))
from manuscript_focus_check import (
    local_neighbour_ratios, robust_stats, robust_z, ink_deficit,
    manuscript_level, describe_side_pattern, parse_segment_and_position,
    object_kind, SEPARATE_GROUPS, SEPARATE_NOTES, region_grid, needs_review,
    ROI_CENTRAL,
)

Image.MAX_IMAGE_PIXELS = None

OUTPUT_DIR = Path(__file__).resolve().parent / "output"

COVER_TITLE_RE = re.compile(
    r"(?i)^\s*(front cover|back cover|spine|fore ?edge|head|tail)\s*$")


def classify(title: str) -> str:
    return "cover" if COVER_TITLE_RE.match(title or "") else "content"


EXTERIOR_ASPECT = 2.5
COVER_DARK_REL = 0.60


def classify_item(rows, brightness_of):
    group = {}
    for r in rows:
        k = key_of(r)
        try:
            w, h = int(r.get("media.width") or 0), int(r.get("media.height") or 0)
        except ValueError:
            w = h = 0
        kind = object_kind(k, r.get("Title", ""))
        if kind:
            group[k] = kind
        elif classify(r.get("Title", "")) == "cover":
            group[k] = "cover"
        elif w and h and max(w, h) / min(w, h) > EXTERIOR_ASPECT:
            group[k] = "exterior"
        else:
            group[k] = "content"
    bvals = [brightness_of[key_of(r)] for r in rows
             if group[key_of(r)] == "content" and brightness_of.get(key_of(r)) is not None]
    if bvals:
        mb = float(np.median(bvals))
        for seq_order in (rows, list(reversed(rows))):
            for r in seq_order:
                k = key_of(r)
                if group[k] == "exterior" or group[k] in SEPARATE_GROUPS:
                    continue
                b = brightness_of.get(k)
                if b is not None and b < COVER_DARK_REL * mb:
                    group[k] = "cover"
                elif group[k] == "content":
                    break
    return group


def load_rows(paths):
    rows = []
    for p in paths:
        with open(p, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("IIIF Access URL"):
                    rows.append(r)
    return rows


def seq_of(row):
    try:
        return int(row.get("Item Sequence") or 0)
    except ValueError:
        return 0


def fetch(url, cache_dir: Path, retries=3, timeout=60):
    key = hashlib.sha1(url.encode()).hexdigest()[:20]
    cached = cache_dir / f"{key}.jpg"
    if cached.exists():
        return cached.read_bytes()
    delay = 3.0
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = resp.read()
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
            return data
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(delay)
            delay *= 2


def measure_bytes(data):
    with Image.open(io.BytesIO(data)) as im:
        arr = np.asarray(im.convert("RGB"), dtype=np.float64)
    gray = 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]
    return float(laplace(gray).var()), float(arr.mean()), ink_deficit(gray), region_grid(gray)


def image_url(row, size, roi):
    try:
        native_w = int(row.get("media.width") or 0)
    except ValueError:
        native_w = 0
    if roi:
        l, t, rw, rh = roi
        region = f"pct:{l*100:g},{t*100:g},{rw*100:g},{rh*100:g}"
        region_w = int(native_w * rw) if native_w else 0
    else:
        region, region_w = "full", native_w
    w = min(size, region_w) if region_w else size
    return f"{row['IIIF Access URL']}/{region}/{w},/0/default.jpg"


def score_one(args):
    row, size, cache_dir, roi = args
    try:
        return (row,) + measure_bytes(fetch(image_url(row, size, roi), cache_dir)) + (None,)
    except Exception as e:
        return row, None, None, None, None, f"{e}"[:150]


def spot_check_sheet(ark_label, ordered_rows, score_by_key, local_map,
                      out_dir: Path, cache_dir: Path, n_pages, crop_px, roles=None):
    cands = [(r, local_map.get(key_of(r), (None, None))[1]) for r in ordered_rows]
    cands = [(r, v) for r, v in cands if v is not None]
    if not cands:
        return None
    ratios = sorted(v for _, v in cands)
    med = ratios[len(ratios) // 2]
    cands.sort(key=lambda rv: abs(rv[1] - med))

    tiles = []
    for row, ratio in cands[:n_pages]:
        try:
            w = int(row.get("media.width") or 0)
            h = int(row.get("media.height") or 0)
        except ValueError:
            w = h = 0
        if w and h:
            cw, ch = min(crop_px, w), min(crop_px, h)
            region = f"{(w - cw) // 2},{(h - ch) // 2},{cw},{ch}"
        else:
            region = "full"
        url = f"{row['IIIF Access URL']}/{region}/full/0/default.jpg"
        try:
            with Image.open(io.BytesIO(fetch(url, cache_dir))) as im:
                tiles.append((row, ratio, im.convert("RGB").copy()))
        except Exception:
            continue
    if not tiles:
        return None

    label_h = 26
    tw = max(t[2].width for t in tiles)
    th = max(t[2].height for t in tiles)
    cols = min(len(tiles), 3)
    rows_n = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (tw * cols, (th + label_h) * rows_n), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    for i, (row, ratio, im) in enumerate(tiles):
        x, y = (i % cols) * tw, (i // cols) * (th + label_h)
        role = (roles or {}).get(key_of(row))
        label = (f"{row.get('Title','?')}  seq {row.get('Item Sequence','?')}  {role}" if role
                 else f"{row.get('Title','?')}  local_ratio={ratio:.2f}")
        draw.text((x + 4, y + 6), label, fill="black", font=font)
        sheet.paste(im, (x, y + label_h))

    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", ark_label)
    out = out_dir / f"SPOTCHECK__{safe}.jpg"
    sheet.save(out, quality=92)
    return out


def key_of(row):
    return row.get("File Name") or row.get("IIIF Access URL")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csvs", nargs="+", type=Path, help="one or more Pages CSV exports")
    ap.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="where each batch's results folder is created (default output/)")
    ap.add_argument("--size", type=int, default=1200,
                     help="requested image width in pixels, capped at native width (default 1200)")
    ap.add_argument("--roi", choices=["central", "full"], default="central",
                     help="measure the central text block (default) or the whole image")
    ap.add_argument("--cache-dir", type=Path, default=OUTPUT_DIR / ".iiif_cache",
                     help="image cache; re-runs reuse it (default output/.iiif_cache)")
    ap.add_argument("--workers", type=int, default=6,
                     help="concurrent downloads (default 6)")
    ap.add_argument("--local-window", type=int, default=1, help="neighbours on each side for the local comparison (default 1)")
    ap.add_argument("--local-ratio-threshold", type=float, default=0.70, help="flag a page below this fraction of its neighbours (default 0.70)")
    ap.add_argument("--threshold", type=float, default=3.5,
                     help="item-wide outlier cutoff, modified z-score (default 3.5)")
    ap.add_argument("--cover-threshold", type=float, default=2.0, help="outlier cutoff for the cover group (default 2.0)")
    ap.add_argument("--no-spot-check", action="store_true", help="skip the spot-check sheets")
    ap.add_argument("--spot-check-pages", type=int, default=2, help="ordinary pages per sheet, one per side (default 2)")
    ap.add_argument("--spot-check-crop", type=int, default=768, help="crop size in pixels for spot-check tiles (default 768)")
    args = ap.parse_args()
    roi = ROI_CENTRAL if args.roi == "central" else None
    for i, csv_path in enumerate(args.csvs):
        if len(args.csvs) > 1:
            print(f"\n=== {csv_path.name} ({i + 1} of {len(args.csvs)}) ===")
        run_batch(args, roi, csv_path)


RESULT_NAMES = {"report.csv", "report-manuscripts.csv", "summary.txt", "spot"}


def prepare_dir(d):
    if d.exists():
        other = {p.name for p in d.iterdir() if not p.name.startswith(".")} - RESULT_NAMES
        if other:
            raise SystemExit(f"{d} already exists and contains other files "
                             f"({', '.join(sorted(other))}); not overwriting.")
        shutil.rmtree(d)
    d.mkdir(parents=True)


def run_batch(args, roi, csv_path):
    out_dir = args.output_dir / csv_path.stem
    args.output = out_dir / "report.csv"
    args.spot_check = None if args.no_spot_check else out_dir / "spot"

    rows = load_rows([csv_path])
    by_ark = defaultdict(list)
    for r in rows:
        by_ark[r.get("Parent ARK", "?")].append(r)
    for ark in by_ark:
        by_ark[ark].sort(key=seq_of)
    print(f"{len(rows)} images across {len(by_ark)} manuscripts. Fetching and scoring...")

    work = [(r, args.size, args.cache_dir, roi) for r in rows]
    scores, bright, ink, grids, errors = {}, {}, {}, {}, {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for row, sc, br, ik, gr, err in ex.map(score_one, work):
            done += 1
            k = key_of(row)
            if err:
                errors[k] = err
            else:
                scores[k], bright[k], ink[k], grids[k] = sc, br, ik, gr
            if done % 25 == 0 or done == len(work):
                print(f"  {done}/{len(work)}", end="\r", file=sys.stderr)
    print(file=sys.stderr)

    out_rows, summaries, spot_inputs = [], [], {}
    for ark, items in sorted(by_ark.items()):
        group_of = classify_item(items, bright)
        rows_by_key = {}
        for group in ("content", "cover", "exterior") + SEPARATE_GROUPS:
            g = [r for r in items if group_of[key_of(r)] == group and key_of(r) in scores]
            if not g:
                continue
            sbp = {key_of(r): scores[key_of(r)] for r in g}
            lm = (local_neighbour_ratios([key_of(r) for r in g], sbp, args.local_window)
                  if group == "content" else {})
            vals = list(sbp.values())
            med, mad = robust_stats(vals) if len(vals) >= 3 else (None, None)
            thr = args.threshold if group == "content" else args.cover_threshold
            for r in g:
                k = key_of(r)
                sc = sbp[k]
                mz = robust_z(med, mad, sc) if med is not None else None
                ref, lr = lm.get(k, (None, None))
                gflag = group == "content" and mz is not None and mz < -thr
                lflag = lr is not None and lr < args.local_ratio_threshold
                reasons = (["local"] if lflag else []) + (["item-outlier"] if gflag else [])
                rec = {
                    "manuscript": ark, "group": group,
                    "sequence": r.get("Item Sequence", ""), "title": r.get("Title", ""),
                    "file": k, "sharpness_score": round(sc, 2),
                    "brightness": round(bright[k], 1), "ink": round(ink[k], 2),
                    "group_median": round(med, 2) if med is not None else "",
                    "modified_z": round(mz, 2) if mz is not None else "",
                    "local_neighbour_ref": round(ref, 2) if ref is not None else "",
                    "local_ratio": round(lr, 3) if lr is not None else "",
                    "flagged_possibly_out_of_focus": "YES" if (gflag or lflag) else "",
                    "flag_reason": "+".join(reasons),
                    "page_note": SEPARATE_NOTES.get(group, ""),
                    "iiif_url": r.get("IIIF Access URL", ""),
                    "width": r.get("media.width", ""), "height": r.get("media.height", ""),
                    "error": "",
                }
                rows_by_key[k] = rec
                out_rows.append(rec)

        pages = []
        for r in items:
            k = key_of(r)
            if k not in rows_by_key:
                continue
            seg, pos = parse_segment_and_position(Path(k))
            tok = (f"{seg}_{pos}" if seg in ("b", "f", "y") and pos else
                   (Path(k).stem.split("_")[-1] if seg == "frag" else None))
            rec = rows_by_key[k]
            pages.append({"id": k, "seq": seq_of(r), "score": scores[k],
                          "brightness": bright[k], "ink": ink[k], "pos": tok,
                          "group": rec["group"], "local_flag": "local" in rec["flag_reason"],
                          "grid": grids.get(k),
                          "local_ratio": rec["local_ratio"] if rec["local_ratio"] != "" else None})
        pat, notes = manuscript_level(pages)
        for k, note in notes.items():
            rows_by_key[k]["page_note"] = note

        for r in items:
            if key_of(r) in errors:
                out_rows.append({
                    "manuscript": ark, "group": "", "sequence": r.get("Item Sequence", ""),
                    "title": r.get("Title", ""), "file": key_of(r), "sharpness_score": "",
                    "brightness": "", "ink": "", "group_median": "", "modified_z": "",
                    "local_neighbour_ref": "", "local_ratio": "",
                    "flagged_possibly_out_of_focus": "", "flag_reason": "", "page_note": "",
                    "iiif_url": r.get("IIIF Access URL", ""),
                    "width": r.get("media.width", ""), "height": r.get("media.height", ""),
                    "error": errors[key_of(r)]})

        recs = [x for x in out_rows if x["manuscript"] == ark]
        flagged = [x for x in recs if x["flagged_possibly_out_of_focus"] == "YES"]
        summaries.append({
            "manuscript": ark, "images": len(items),
            "covers": sum(1 for x in recs if x["group"] == "cover"),
            "exterior": sum(1 for x in recs if x["group"] == "exterior"),
            "content": sum(1 for x in recs if x["group"] == "content"),
            "likely_blank": sum(1 for x in recs if x["page_note"].startswith("likely blank")),
            "side_pattern": pat["verdict"] if pat else "",
            "side_pattern_detail": describe_side_pattern(pat) if pat else "",
            "flags": len(flagged),
            "flags_explained_by_pattern": sum(1 for x in flagged if x["page_note"].startswith("one instance")),
            "flags_on_blank_pages": sum(1 for x in flagged if x["page_note"].startswith("likely blank")),
            "fragments_for_visual_check": sum(1 for x in recs if x["group"] == "fragment"),
            "inserts": sum(1 for x in recs if x["group"] == "insert"),
            "flags_needing_individual_review": sum(1 for x in flagged if needs_review(x["page_note"])),
            "possible_lift_or_tilt": sum(1 for x in flagged if "possible lift" in x["page_note"]),
            "fetch_errors": sum(1 for x in recs if x["error"]),
        })
        spot_inputs[ark] = (items, rows_by_key)

    prepare_dir(out_dir)
    out_rows.sort(key=lambda r: (r["manuscript"], int(r["sequence"] or 0)))
    fields = list(out_rows[0].keys())
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n", quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(out_rows)
    summary_path = args.output.with_name(args.output.stem + "-manuscripts.csv")
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(summaries[0].keys()), lineterminator="\n", quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(summaries)

    lines = ["\nManuscript-level results (read these before the page flags):"]
    for sm in summaries:
        lines.append(f"\n  {sm['manuscript']}: {sm['images']} images "
                     f"({sm['covers']} cover, {sm['exterior']} exterior, {sm['content']} content, "
                     f"{sm['likely_blank']} likely blank, {sm['fragments_for_visual_check']} fragments, "
                     f"{sm['inserts']} inserts)")
        lines.append(f"    side pattern: {sm['side_pattern_detail'] or 'not assessed'}")
        lines.append(f"    {sm['flags']} page flags: {sm['flags_explained_by_pattern']} explained by the "
                     f"pattern, {sm['flags_on_blank_pages']} on blank pages, "
                     f"{sm['flags_needing_individual_review']} need individual review"
                     + (f" ({sm['possible_lift_or_tilt']} possible lift or tilt)" if sm['possible_lift_or_tilt'] else "")
                     + (f"; {sm['fetch_errors']} fetch errors" if sm['fetch_errors'] else ""))
    for line in lines:
        print(line)
    header = [f"{csv_path.name} - {len(rows)} images across {len(by_ark)} manuscripts",
              f"run {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    (out_dir / "summary.txt").write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    print(f"\nResults in {out_dir}/: report.csv, report-manuscripts.csv, summary.txt"
          + ("" if args.no_spot_check else ", spot/"))

    if args.spot_check:
        made = 0
        for ark, (items, rows_by_key) in spot_inputs.items():
            cands = [r for r in items if key_of(r) in rows_by_key
                     and rows_by_key[key_of(r)]["group"] == "content"
                     and not rows_by_key[key_of(r)]["page_note"].startswith("likely blank")]
            picks = []
            for parity in (1, 0):
                side = [r for r in cands if seq_of(r) % 2 == parity]
                if side:
                    m = float(np.median([scores[key_of(r)] for r in side]))
                    picks.append(min(side, key=lambda r: abs(scores[key_of(r)] - m)))
            picks = picks[:max(1, args.spot_check_pages)]
            roles = {key_of(r): f"ordinary {'odd' if seq_of(r) % 2 else 'even'} page"
                     for r in picks}
            frags = [r for r in items if key_of(r) in rows_by_key
                     and rows_by_key[key_of(r)]["group"] == "fragment"]
            for r in frags:
                roles[key_of(r)] = "fragment - judge focus directly"
            tiles = picks + frags
            lm = {key_of(r): (None, 1.0) for r in tiles}
            if tiles and spot_check_sheet(ark, tiles, {}, lm, args.spot_check, args.cache_dir,
                                            len(tiles), args.spot_check_crop, roles=roles):
                made += 1
        print(f"\nSpot-check sheets: {made} written to {args.spot_check}/")
        print("  One ORDINARY page from each side of the opening, plus every fragment, at native")
        print("  resolution. Fragments are not compared with pages -- judge their focus directly.")
        print("  If both look soft, the whole item may be out of focus -- no relative check sees that.")


if __name__ == "__main__":
    main()
