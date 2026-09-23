"""Focus QC for IMI manuscript images on disk (TIFF). See README.md."""
import argparse
import csv
import re
import sys
from pathlib import Path
from multiprocessing import Pool, cpu_count

from PIL import Image, ImageDraw, ImageFont
import numpy as np
from scipy.ndimage import laplace

Image.MAX_IMAGE_PIXELS = None

OUTPUT_DIR = Path(__file__).resolve().parent / "output"

RESIZE_MAX_DIM = 1500
TIFF_GLOB_PATTERNS = ("*.tif", "*.tiff", "*.TIF", "*.TIFF")

KNOWN_SEGMENTS = {"a", "b", "f", "y", "z"}
FRAG_RE = re.compile(r"(?i)^frag\d+[rv]?$")


def natural_sort_key(name: str):
    return [int(chunk) if chunk.isdigit() else chunk.lower()
            for chunk in re.split(r"(\d+)", name)]


def parse_segment_and_position(path: Path):
    stem = path.stem
    parts = stem.split("_")
    if not parts:
        return None, None
    last = parts[-1]
    if FRAG_RE.match(last):
        return "frag", None
    if len(parts) >= 2 and parts[-2].lower() in KNOWN_SEGMENTS:
        return parts[-2].lower(), last
    return None, None


def leading_int(s):
    if not s:
        return None
    m = re.match(r"\d+", s)
    return int(m.group()) if m else None


ROI_CENTRAL = (0.20, 0.15, 0.60, 0.70)


def ink_deficit(gray):
    return float(np.percentile(gray, 95) - gray.mean())


GRID = 3


def region_grid(gray, n=GRID):
    lap = laplace(gray)
    h, w = lap.shape
    return [float(lap[r * h // n:(r + 1) * h // n, c * w // n:(c + 1) * w // n].var())
            for r in range(n) for c in range(n)]


def analyze_image(path: Path, roi=ROI_CENTRAL):
    with Image.open(path) as img:
        img_rgb = img.convert("RGB")
        if roi:
            W0, H0 = img_rgb.size
            l, t, rw, rh = roi
            img_rgb = img_rgb.crop((int(W0 * l), int(H0 * t),
                                    int(W0 * (l + rw)), int(H0 * (t + rh))))
        w, h = img_rgb.size
        scale = min(1.0, RESIZE_MAX_DIM / max(w, h))
        if scale < 1.0:
            img_rgb = img_rgb.resize((int(w * scale), int(h * scale)), Image.BILINEAR)
        arr = np.asarray(img_rgb, dtype=np.float64)

    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    gray = 0.299 * r + 0.587 * g + 0.114 * b
    sharpness = float(laplace(gray).var())

    r_mean, g_mean, b_mean = r.mean(), g.mean(), b.mean()
    brightness = (r_mean + g_mean + b_mean) / 3
    mx, mn = max(r_mean, g_mean, b_mean), min(r_mean, g_mean, b_mean)
    saturation = 0.0 if mx == 0 else (mx - mn) / mx
    ink = ink_deficit(gray)

    return sharpness, brightness, saturation, ink, region_grid(gray)


def process_one(args):
    folder_name, group_name, path_str, roi = args
    path = Path(path_str)
    try:
        sharpness, brightness, saturation, ink, grid = analyze_image(path, roi)
        return (folder_name, group_name, str(path), sharpness, brightness, saturation, ink, grid, None)
    except Exception as e:
        return (folder_name, group_name, str(path), None, None, None, None, None, str(e))


def find_manuscript_folders(root: Path):
    return sorted([d for d in root.iterdir() if d.is_dir()])


def find_tiffs_in_folder(folder: Path):
    files = []
    for pattern in TIFF_GLOB_PATTERNS:
        files.extend(folder.glob(pattern))
    seen = set()
    unique = []
    for f in files:
        if f.name not in seen:
            seen.add(f.name)
            unique.append(f)
    return sorted(unique, key=lambda p: natural_sort_key(p.name))


def classify_folder(pages, cover_segments, exclude_re, skip_first, skip_last):
    segments = {p: parse_segment_and_position(p) for p in pages}
    structured = any(seg is not None for p, (seg, _pos) in segments.items()
                     if not object_kind(p))

    groups = {"content": [], "cover": []}
    excluded = []
    reasons = {}
    n = len(pages)

    if structured:
        for p in pages:
            if exclude_re and exclude_re.search(p.name):
                excluded.append(p)
                reasons[p] = "pattern"
                continue
            kind = object_kind(p)
            if kind:
                groups.setdefault(kind, []).append(p)
                continue
            seg, pos = segments[p]
            if seg is not None and seg in cover_segments:
                pos_num = leading_int(pos)
                if seg == "a":
                    if pos_num == 1:
                        groups["cover"].append(p)
                    else:
                        groups["content"].append(p)
                elif seg == "z":
                    if pos_num == 1:
                        groups["content"].append(p)
                    else:
                        groups["cover"].append(p)
                else:
                    groups["cover"].append(p)
            else:
                groups["content"].append(p)
        mode = ("structured (segment-code naming) -- covers vs covers, pages vs pages; "
                "inside front/back cover positions folded into content")
    else:
        for i, p in enumerate(pages):
            if exclude_re and exclude_re.search(p.name):
                excluded.append(p)
                reasons[p] = "pattern"
                continue
            kind = object_kind(p)
            if kind:
                groups.setdefault(kind, []).append(p)
                continue
            if (skip_first and i < skip_first) or (skip_last and i >= n - skip_last):
                groups["cover"].append(p)
            else:
                groups["content"].append(p)
        if skip_first or skip_last:
            mode = "bare-sequence (positional head/tail = cover group)"
        else:
            mode = "bare-sequence (no head/tail configured -- single group)"

    groups = {k: v for k, v in groups.items() if v}
    excluded_set = set(excluded)
    scored_in_order = [p for p in pages if p not in excluded_set]
    return groups, excluded, reasons, mode, scored_in_order


def robust_stats(values):
    arr = np.array(values, dtype=np.float64)
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))
    return median, mad


def robust_z(median, mad, x):
    if mad == 0:
        mad = 1e-9
    return 0.6745 * (x - median) / mad


def local_neighbour_ratios(ordered_paths, score_by_path, window):
    out = {}
    scores = [score_by_path.get(str(p)) for p in ordered_paths]
    n = len(ordered_paths)
    for i, p in enumerate(ordered_paths):
        s_i = scores[i]
        if s_i is None:
            continue
        nb = [scores[j] for j in range(max(0, i - window), min(n, i + window + 1))
              if j != i and scores[j] is not None]
        if not nb:
            continue
        ref = float(np.median(nb))
        if ref <= 0:
            continue
        out[str(p)] = (ref, s_i / ref)
    return out


def make_spot_check_sheet(folder_name, ordered_paths, score_by_path, local_map,
                           output_dir: Path, n_pages, crop_px, role=None):
    scored = [(p, local_map.get(str(p), (None, None))[1]) for p in ordered_paths]
    scored = [(p, r) for p, r in scored if r is not None]
    if not scored:
        return None
    ratios = sorted(r for _, r in scored)
    med_ratio = ratios[len(ratios) // 2]
    scored.sort(key=lambda pr: abs(pr[1] - med_ratio))
    picks = scored[:n_pages]

    tiles = []
    for p, ratio in picks:
        try:
            with Image.open(p) as im:
                im = im.convert("RGB")
                w, h = im.size
                cw = min(crop_px, w)
                ch = min(crop_px, h)
                left = (w - cw) // 2
                top = (h - ch) // 2
                crop = im.crop((left, top, left + cw, top + ch))
            tiles.append((p, ratio, crop))
        except Exception:
            continue
    if not tiles:
        return None

    label_h = 26
    tile_w = max(t[2].width for t in tiles)
    tile_h = max(t[2].height for t in tiles)
    cols = min(len(tiles), 3)
    rows_n = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (tile_w * cols, (tile_h + label_h) * rows_n), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    for i, (p, ratio, crop) in enumerate(tiles):
        cx = (i % cols) * tile_w
        cy = (i // cols) * (tile_h + label_h)
        label = f"{p.name}  {role}" if role else f"{p.name}  local_ratio={ratio:.2f}"
        draw.text((cx + 4, cy + 6), label, fill="black", font=font)
        sheet.paste(crop, (cx, cy + label_h))

    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / f"SPOTCHECK__{folder_name}.jpg"
    sheet.save(out, quality=92)
    return out


def make_contact_sheet(flagged_path: Path, folder_pages, output_dir: Path, label_scores: dict):
    try:
        idx = folder_pages.index(flagged_path)
    except ValueError:
        return
    window = folder_pages[max(0, idx - 1): idx + 2]

    thumb_w = 350
    thumbs = []
    for p in window:
        try:
            with Image.open(p) as im:
                im = im.convert("RGB")
                ratio = thumb_w / im.width
                im = im.resize((thumb_w, int(im.height * ratio)), Image.BILINEAR)
                thumbs.append((p, im))
        except Exception:
            continue
    if not thumbs:
        return

    max_h = max(im.height for _, im in thumbs)
    label_h = 30
    sheet = Image.new("RGB", (thumb_w * len(thumbs), max_h + label_h), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    for i, (p, im) in enumerate(thumbs):
        x = i * thumb_w
        sheet.paste(im, (x, label_h))
        tag = " <-- FLAGGED" if p == flagged_path else ""
        score = label_scores.get(str(p))
        score_str = f"{score:.1f}" if score is not None else "?"
        draw.text((x + 5, 5), f"{p.name}  score={score_str}{tag}", fill="black", font=font)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"{flagged_path.parent.name}__{flagged_path.stem}.jpg"
    sheet.save(output_dir / out_name, quality=85)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("collection_dir", type=Path,
                     help="folder containing one subfolder per manuscript")
    ap.add_argument("--output", type=Path, default=OUTPUT_DIR / "focus_report.csv", help="page report CSV (default output/focus_report.csv)")
    ap.add_argument("--cover-segments", type=str, default="a,z",
                     help="segment codes treated as cover/exterior in coded items (default a,z)")
    ap.add_argument("--skip-first", type=int, default=0,
                     help="bare-sequence items: treat the first N images as covers")
    ap.add_argument("--skip-last", type=int, default=0,
                     help="bare-sequence items: treat the last N images as covers/exterior")
    ap.add_argument("--exclude-pattern", type=str, default=None,
                     help="regex; matching filenames are left out entirely (e.g. colour targets)")
    ap.add_argument("--threshold", type=float, default=3.5,
                     help="item-wide outlier cutoff, modified z-score (default 3.5)")
    ap.add_argument("--cover-threshold", type=float, default=None,
                     help="outlier cutoff for the cover group (default: same as --threshold)")
    ap.add_argument("--roi", choices=["central", "full"], default="central",
                     help="measure the central text block (default) or the whole image")
    ap.add_argument("--local-window", type=int, default=1,
                     help="neighbours on each side for the local comparison (default 1)")
    ap.add_argument("--local-ratio-threshold", type=float, default=0.70,
                     help="flag a page below this fraction of its neighbours (default 0.70)")
    ap.add_argument("--disable-local", action="store_true",
                     help="use only the item-wide outlier test")
    ap.add_argument("--color-check", action="store_true",
                     help="add colour-distinctiveness columns (informational only)")
    ap.add_argument("--color-threshold", type=float, default=2.5,
                     help="cutoff for --color-check (default 2.5)")
    ap.add_argument("--workers", type=int, default=max(1, cpu_count() - 1), help="parallel processes (default: CPUs - 1)")
    ap.add_argument("--spot-check", type=Path, nargs="?", const=OUTPUT_DIR / "spot", default=None,
                     help="write spot-check sheets (default folder output/spot)")
    ap.add_argument("--spot-check-pages", type=int, default=3,
                     help="ordinary pages per spot-check sheet (default 3)")
    ap.add_argument("--spot-check-crop", type=int, default=768,
                     help="crop size in pixels for spot-check tiles (default 768)")
    ap.add_argument("--contact-sheets", type=Path, default=None,
                     help="write a sheet per flagged page (with neighbours) to this folder")
    ap.add_argument("--folder-ratio-threshold", type=float, default=0.0,
                     help="flag items whose median is below this fraction of the batch median (default 0 = off)")
    ap.add_argument("--folder-ratio-min-folders", type=int, default=4,
                     help="minimum items before --folder-ratio-threshold applies (default 4)")
    ap.add_argument("--folder-summary-output", type=Path, default=None,
                     help="per-manuscript summary CSV (default <output>-folders.csv)")
    args = ap.parse_args()
    roi = ROI_CENTRAL if args.roi == "central" else None

    exclude_re = re.compile(args.exclude_pattern, re.IGNORECASE) if args.exclude_pattern else None
    cover_segments = {s.strip().lower() for s in args.cover_segments.split(",") if s.strip()}
    cover_threshold = args.cover_threshold if args.cover_threshold is not None else args.threshold

    folders = find_manuscript_folders(args.collection_dir)
    if not folders:
        print(f"No subfolders found in {args.collection_dir}", file=sys.stderr)
        sys.exit(1)

    folder_groups = {}
    folder_excluded = {}
    folder_reasons = {}
    folder_mode = {}
    folder_scored_order = {}
    work_items = []

    for folder in folders:
        pages = find_tiffs_in_folder(folder)
        groups, excluded, reasons, mode, scored_order = classify_folder(
            pages, cover_segments, exclude_re, args.skip_first, args.skip_last
        )
        folder_groups[folder.name] = groups
        folder_excluded[folder.name] = excluded
        folder_reasons[folder.name] = reasons
        folder_mode[folder.name] = mode
        folder_scored_order[folder.name] = scored_order
        for group_name, paths in groups.items():
            work_items.extend((folder.name, group_name, str(p), roi) for p in paths)

    total_files = len(work_items) + sum(len(v) for v in folder_excluded.values())
    print(f"Found {len(folders)} manuscript folders, {total_files} TIFFs "
          f"({len(work_items)} to score, {total_files - len(work_items)} fully excluded).")
    for name in sorted(folder_mode):
        counts = ", ".join(f"{g}={len(p)}" for g, p in folder_groups[name].items())
        print(f"  {name}: {folder_mode[name]}  [{counts}, excluded={len(folder_excluded[name])}]")

    if not work_items:
        print("Nothing to score.", file=sys.stderr)
        sys.exit(1)

    print(f"\nScoring with {args.workers} workers...")
    results = []
    with Pool(args.workers) as pool:
        for i, res in enumerate(pool.imap_unordered(process_one, work_items), 1):
            results.append(res)
            if i % 50 == 0 or i == len(work_items):
                print(f"  {i}/{len(work_items)}", end="\r", file=sys.stderr)
    print()

    by_group = {}
    by_folder_color = {}

    for folder_name, group_name, path_str, sharpness, brightness, saturation, _ink, _grid, err in results:
        by_group.setdefault((folder_name, group_name), []).append(
            (path_str, sharpness, err)
        )
        by_folder_color.setdefault(folder_name, []).append(
            (path_str, group_name, brightness, saturation, err)
        )

    color_distance = {}
    color_flag = {}
    if args.color_check:
        for folder_name, entries in by_folder_color.items():
            valid = [(p, br, sa) for p, g, br, sa, e in entries if e is None]
            if len(valid) < 3:
                continue
            brs = [v[1] for v in valid]
            sas = [v[2] for v in valid]
            br_med, br_mad = robust_stats(brs)
            sa_med, sa_mad = robust_stats(sas)
            for p, br, sa in valid:
                zb = robust_z(br_med, br_mad, br)
                zs = robust_z(sa_med, sa_mad, sa)
                dist = float(np.sqrt(zb ** 2 + zs ** 2))
                color_distance[p] = dist
                color_flag[p] = dist > args.color_threshold

    rows = []
    flagged_paths = []
    all_scores_by_path = {}
    local_by_folder = {}
    scores_by_folder = {}
    folder_content_median = {}
    folder_content_count = {}

    for (folder_name, group_name), entries in sorted(by_group.items()):
        scored = [(p, s) for p, s, e in entries if s is not None]
        errored = [(p, e) for p, s, e in entries if e is not None]

        if len(scored) < 3:
            median = mad = None
            mod_zs = {p: None for p, _ in scored}
        else:
            scores_only = [s for _, s in scored]
            median, mad = robust_stats(scores_only)
            mod_zs = {p: robust_z(median, mad, s) for p, s in scored}

        if group_name == "content" and median is not None:
            folder_content_median[folder_name] = median
            folder_content_count[folder_name] = len(scored)

        active_threshold = cover_threshold if group_name == "cover" else args.threshold

        score_by_path = {p: sc for p, sc in scored}
        local_map = {}
        if not args.disable_local and group_name not in SEPARATE_GROUPS:
            ordered = folder_groups.get(folder_name, {}).get(group_name, [])
            local_map = local_neighbour_ratios(ordered, score_by_path, args.local_window)
        if group_name == "content":
            local_by_folder.setdefault(folder_name, {}).update(local_map)
            scores_by_folder.setdefault(folder_name, {}).update(score_by_path)

        for p, s in scored:
            all_scores_by_path[p] = s
            mz = mod_zs.get(p)
            global_flag = (mz is not None and mz < -active_threshold)

            lref, lratio = local_map.get(p, (None, None))
            local_flag = (lratio is not None and lratio < args.local_ratio_threshold)

            if group_name in SEPARATE_GROUPS:
                global_flag = local_flag = False
            flagged = global_flag or local_flag
            reasons = []
            if local_flag:
                reasons.append("local")
            if global_flag:
                reasons.append("folder-outlier")
            if flagged:
                flagged_paths.append((Path(p), folder_name))

            row = {
                "folder": folder_name, "group": group_name, "file": Path(p).name, "path": p,
                "sharpness_score": round(s, 2),
                "group_median": round(median, 2) if median is not None else "",
                "group_mad": round(mad, 2) if mad is not None else "",
                "modified_z": round(mz, 2) if mz is not None else "",
                "local_neighbour_ref": round(lref, 2) if lref is not None else "",
                "local_ratio": round(lratio, 3) if lratio is not None else "",
                "flagged_possibly_out_of_focus": "YES" if flagged else "",
                "flag_reason": "+".join(reasons),
                "excluded_reason": "", "error": "",
                "color_distance": "", "color_note": "",
            }
            if args.color_check and p in color_distance:
                row["color_distance"] = round(color_distance[p], 2)
                if group_name == "content" and color_flag.get(p):
                    row["color_note"] = ("REVIEW: color-distinct content-group page -- "
                                          "could be illumination/decoration, a blank leaf, "
                                          "a misnamed file, or a missed cover; check manually")
            rows.append(row)

        for p, e in errored:
            rows.append({
                "folder": folder_name, "group": group_name, "file": Path(p).name, "path": p,
                "sharpness_score": "", "group_median": "", "group_mad": "",
                "modified_z": "", "local_neighbour_ref": "", "local_ratio": "",
                "flagged_possibly_out_of_focus": "", "flag_reason": "",
                "excluded_reason": "", "error": e,
                "color_distance": "", "color_note": "",
            })
        for p in folder_excluded.get(folder_name, []):
            rows.append({
                "folder": folder_name, "group": "excluded", "file": p.name, "path": str(p),
                "sharpness_score": "", "group_median": "", "group_mad": "",
                "modified_z": "", "local_neighbour_ref": "", "local_ratio": "",
                "flagged_possibly_out_of_focus": "", "flag_reason": "",
                "excluded_reason": folder_reasons[folder_name].get(p, "pattern"), "error": "",
                "color_distance": "", "color_note": "",
            })

    bright_by_path = {r[2]: r[4] for r in results if r[8] is None}
    ink_by_path = {r[2]: r[6] for r in results if r[8] is None}
    grid_by_path = {r[2]: r[7] for r in results if r[8] is None}
    ms_patterns = {}
    for folder_name, order in folder_scored_order.items():
        index_of = {str(pp): i + 1 for i, pp in enumerate(order)}
        frows = [r for r in rows if r["folder"] == folder_name and r["sharpness_score"] != ""]
        pages = []
        for r in frows:
            seg, pos = parse_segment_and_position(Path(r["path"]))
            tok = f"{seg}_{pos}" if seg in ("b", "f", "y") and pos else (
                Path(r["path"]).stem.split("_")[-1] if seg == "frag" else None)
            pages.append({"id": r["path"], "seq": index_of.get(r["path"], 0),
                          "score": float(r["sharpness_score"]),
                          "brightness": bright_by_path.get(r["path"]),
                          "ink": ink_by_path.get(r["path"]),
                          "grid": grid_by_path.get(r["path"]),
                          "local_ratio": float(r["local_ratio"]) if r.get("local_ratio") not in ("", None) else None,
                          "pos": tok, "group": r["group"], 
                          "local_flag": "local" in (r.get("flag_reason") or "")})
        pat, notes = manuscript_level(pages)
        ms_patterns[folder_name] = pat
        for r in frows:
            r["page_note"] = notes.get(r["path"], "")
    for r in rows:
        r.setdefault("page_note", "")
        if r["group"] in SEPARATE_GROUPS:
            r["page_note"] = SEPARATE_NOTES[r["group"]]

    rows.sort(key=lambda r: (
        0 if r["flagged_possibly_out_of_focus"] == "YES" else 1,
        r["modified_z"] if r["modified_z"] != "" else 0,
        r["folder"], r["group"], r["file"],
    ))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        fieldnames = ["folder", "group", "file", "path", "sharpness_score", "group_median",
                      "group_mad", "modified_z", "local_neighbour_ref", "local_ratio",
                      "flagged_possibly_out_of_focus", "flag_reason", "page_note",
                      "excluded_reason", "error", "color_distance", "color_note"]
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    print("\nManuscript-level side patterns (read these before the page flags):")
    for name in sorted(ms_patterns):
        pat = ms_patterns[name]
        print(f"  {name}: {describe_side_pattern(pat) if pat else 'not assessed'}")
    n_explained = sum(1 for r in rows if r["page_note"].startswith("one instance"))
    n_lift = sum(1 for r in rows if "possible lift" in r["page_note"])
    n_blank = sum(1 for r in rows if r["page_note"].startswith("likely blank"))

    n_flagged = sum(1 for r in rows if r["flagged_possibly_out_of_focus"] == "YES")
    n_local = sum(1 for r in rows if "local" in (r.get("flag_reason") or ""))
    n_globalonly = sum(1 for r in rows if r.get("flag_reason") == "folder-outlier")
    n_errors = sum(1 for r in rows if r["error"])
    n_color_notes = sum(1 for r in rows if r["color_note"])
    print(f"\nDone. {n_flagged} pages flagged as possible focus problems, {n_errors} read errors.")
    if not args.disable_local:
        print(f"  {n_local} flagged by local neighbour comparison "
              f"(< {args.local_ratio_threshold:.2f} of neighbour median)")
    print(f"  {n_globalonly} flagged by folder-wide outlier test only")
    print(f"  {n_explained} of the flags are instances of a manuscript-level pattern; "
          f"{n_blank} pages tagged as likely blank")
    n_indep = sum(1 for r in rows if r["flagged_possibly_out_of_focus"] == "YES"
                  and needs_review(r["page_note"]))
    print(f"  {n_indep} flags remain that need individual review"
          + (f" ({n_lift} look like a possible lift or tilt)" if n_lift else ""))
    if args.color_check:
        print(f"{n_color_notes} content-group pages flagged for color-distinctiveness review.")

    print(f"Report: {args.output}")

    folder_rows = []
    baseline = None
    n_folder_flagged = 0

    if folder_content_median:
        medians = list(folder_content_median.values())
        enough_folders = len(medians) >= args.folder_ratio_min_folders
        check_enabled = args.folder_ratio_threshold > 0 and enough_folders
        baseline = float(np.median(medians))

        for name in sorted(folder_content_median):
            med = folder_content_median[name]
            ratio = (med / baseline) if baseline else None
            flagged_folder = bool(check_enabled and ratio is not None
                                   and ratio < args.folder_ratio_threshold)
            if flagged_folder:
                n_folder_flagged += 1
            folder_rows.append({
                "folder": name,
                "content_pages_scored": folder_content_count.get(name, ""),
                "content_median_sharpness": round(med, 2),
                "batch_baseline_median": round(baseline, 2),
                "ratio_to_baseline": round(ratio, 3) if ratio is not None else "",
                "flagged_systematically_soft": "YES" if flagged_folder else "",
                "side_pattern": (ms_patterns.get(name) or {}).get("verdict", ""),
                "side_pattern_detail": describe_side_pattern(ms_patterns[name]) if ms_patterns.get(name) else "",
            })

        summary_path = args.folder_summary_output
        if summary_path is None:
            summary_path = args.output.with_name(args.output.stem + "-folders.csv")
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with open(summary_path, "w", newline="", encoding="utf-8") as f:
            fieldnames = ["folder", "content_pages_scored", "content_median_sharpness",
                          "batch_baseline_median", "ratio_to_baseline",
                          "flagged_systematically_soft", "side_pattern", "side_pattern_detail"]
            writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n", quoting=csv.QUOTE_ALL)
            writer.writeheader()
            writer.writerows(folder_rows)

        print(f"\nCross-folder check: batch baseline (median of folder medians) = "
              f"{baseline:.2f}")
        if not enough_folders:
            print(f"  Skipped flagging: only {len(medians)} folder(s) with content statistics, "
                  f"need >= {args.folder_ratio_min_folders} for a meaningful batch baseline.")
        elif args.folder_ratio_threshold <= 0:
            print("  Flagging disabled (--folder-ratio-threshold 0).")
        elif n_folder_flagged:
            print(f"  {n_folder_flagged} folder(s) below {args.folder_ratio_threshold:.0%} of "
                  f"baseline -- possible whole-item/session focus problem:")
            for fr in folder_rows:
                if fr["flagged_systematically_soft"]:
                    print(f"    {fr['folder']}: median {fr['content_median_sharpness']} "
                          f"({fr['ratio_to_baseline']:.2f}x baseline)")
        else:
            print(f"  No folders below {args.folder_ratio_threshold:.0%} of baseline.")
        print(f"  Folder summary: {summary_path}")

    if args.spot_check:
        written = 0
        for folder_name, groups in folder_groups.items():
            ordered = groups.get("content", [])
            lm = local_by_folder.get(folder_name, {})
            sb = scores_by_folder.get(folder_name, {})
            if not ordered or not lm:
                continue
            out = make_spot_check_sheet(folder_name, ordered, sb, lm, args.spot_check,
                                         args.spot_check_pages, args.spot_check_crop)
            if out:
                written += 1
            frags = groups.get("fragment", [])
            if frags:
                make_spot_check_sheet(f"FRAGMENTS__{folder_name}", frags, {},
                                      {str(fp): (None, 1.0) for fp in frags},
                                      args.spot_check, len(frags), args.spot_check_crop,
                                      role="fragment - judge focus directly")
        print(f"\nSpot-check sheets: {written} written to {args.spot_check}/"
              f" (plus a FRAGMENTS sheet for any item with fragments)")
        print("  These show ORDINARY pages, not flagged ones. If they look soft to you, the")
        print("  whole manuscript may be out of focus -- a relative check cannot detect that.")

    if args.contact_sheets and flagged_paths:
        print(f"Writing {len(flagged_paths)} contact sheets to {args.contact_sheets}/ ...")
        for path, folder_name in flagged_paths:
            make_contact_sheet(path, folder_scored_order[folder_name], args.contact_sheets,
                                all_scores_by_path)
        print("Contact sheets done.")


SIDE_SYSTEMATIC = 0.80
SIDE_CONSISTENT = 0.70
SIDE_MILD = 0.90
SIDE_MILD_CONSISTENT = 0.60
DRIFT_SWING = 1.30


def _side_of(pos_token, seq):
    if pos_token:
        m = re.search(r"(\d+)[a-z]*?([rv])$", pos_token)
        if m:
            return m.group(2), True
    return ("odd" if seq % 2 else "even"), False


def side_pattern(items, window_pairs=10, step_pairs=5):
    tagged = []
    n_known = 0
    for seq, score, tok in items:
        side, k = _side_of(tok, seq)
        n_known += k
        tagged.append((seq, score, side))
    known = bool(tagged) and n_known / len(tagged) >= 0.8
    if not known:
        tagged = [(seq, sc, "odd" if seq % 2 else "even") for seq, sc, _ in tagged]
    if known:
        tagged = [t for t in tagged if t[2] in ("r", "v")]
        a, b, basis = "r", "v", "recto/verso from filenames"
    else:
        a, b, basis = "odd", "even", "sequence parity (sides not encoded)"

    ratios = []
    for i, (seq, sc, side) in enumerate(tagged):
        if side != a or sc <= 0:
            continue
        nb = [tagged[j][1] for j in (i - 1, i + 1)
              if 0 <= j < len(tagged) and tagged[j][2] == b and tagged[j][1] > 0]
        if nb:
            ratios.append((seq, sc / (sum(nb) / len(nb))))

    out = {"basis": basis, "side_a": a, "side_b": b, "n_pairs": len(ratios),
           "ratio_a_over_b": None, "softer_side": None, "severity": None,
           "consistency": None, "windows": [], "verdict": "insufficient"}
    if len(ratios) < 6:
        return out

    vals = np.array([r for _, r in ratios])
    med = float(np.median(vals))
    softer = a if med < 1 else b
    sev = med if med < 1 else 1 / med
    frac = float(np.mean(vals < 1)) if med < 1 else float(np.mean(vals > 1))

    wins = []
    for start in range(0, max(1, len(ratios) - window_pairs + 1), step_pairs):
        chunk = ratios[start:start + window_pairs]
        if len(chunk) < max(5, window_pairs // 2):
            continue
        wm = float(np.median([r for _, r in chunk]))
        wins.append((chunk[0][0], chunk[-1][0], round(wm, 3), len(chunk)))

    if sev < SIDE_SYSTEMATIC and frac >= SIDE_CONSISTENT:
        verdict = "systematic"
    elif sev < SIDE_MILD and frac >= SIDE_MILD_CONSISTENT:
        verdict = "mild"
    else:
        wsev = [(w[2] if w[2] < 1 else 1 / w[2]) for w in wins]
        wr = [w[2] for w in wins]
        reversal = bool(wr) and min(wr) < 1 < max(wr)
        swing = (max(wr) / min(wr)) if wr and min(wr) > 0 else 1.0
        if reversal and swing >= DRIFT_SWING:
            verdict = "drift"
        elif wsev and min(wsev) < SIDE_SYSTEMATIC:
            verdict = "localized"
        else:
            verdict = "none"

    out.update(ratio_a_over_b=round(med, 3), softer_side=softer,
               severity=round(sev, 3), consistency=round(frac, 3),
               windows=wins, verdict=verdict)
    return out


def describe_side_pattern(p):
    if p["verdict"] == "insufficient":
        return f"too few paired pages ({p['n_pairs']}) to assess"
    if p["verdict"] == "drift":
        s = (f"softer side changes partway through ({p['n_pairs']} pairs; {p['basis']})")
    else:
        s = (f"{p['softer_side']} softer: {p['severity']:.2f}x the other side, "
             f"{p['consistency']:.0%} of pairs ({p['n_pairs']} pairs; {p['basis']})")
    if p["windows"]:
        wr = [w[2] for w in p["windows"]]
        lo = min(p["windows"], key=lambda w: w[2] if w[2] < 1 else 1 / w[2])
        s += (f"; {p['side_a']}/{p['side_b']} across the book {min(wr):.2f}-{max(wr):.2f}, "
              f"strongest in seq {lo[0]}-{lo[1]} ({lo[2]:.2f})")
    return f"{p['verdict'].upper()}: {s}"


BLANK_REL = 0.50
BLANK_INK_REL = 0.65


def is_blank(score, content_median, brightness, content_median_brightness,
             ink=None, content_median_ink=None):
    if content_median <= 0 or brightness is None:
        return False
    low_sharp = score < BLANK_REL * content_median
    paper_bright = brightness >= content_median_brightness - 10
    if ink is None or not content_median_ink:
        return False
    return low_sharp and paper_bright and ink < BLANK_INK_REL * content_median_ink


SEPARATE_GROUPS = ("fragment", "insert")


def object_kind(path=None, title=None):
    if title:
        m = re.match(r"(?i)^\s*(fragment|insert)\b", title)
        if m:
            return m.group(1).lower()
    if path is not None:
        tok = Path(str(path)).stem.split("_")[-1].lower()
        if re.match(r"^frag(ment)?\d*[rv]?$", tok):
            return "fragment"
        if re.match(r"^insert\d*[rv]?$", tok):
            return "insert"
    return None


SEPARATE_NOTES = {
    "fragment": "fragment - part of the text block but not comparable to pages "
                "(backing, damage, size); on the spot-check sheet for visual review",
    "insert": "insert - separate object from the text block; not checked for focus",
}


PATTERN_EXPLAINS = 0.70
UNEVEN_RATIO = 0.50
EXPLAINED_PREFIXES = ("one instance", "likely blank", "fragment", "insert")


def needs_review(note):
    return not (note or "").startswith(EXPLAINED_PREFIXES)


def _uneven(page, same_side):
    refs = [p["grid"] for p in same_side if p.get("grid") and p["id"] != page["id"]]
    if len(refs) < 4 or not page.get("grid"):
        return None
    ratios = []
    for k in range(GRID * GRID):
        ref = float(np.median([g[k] for g in refs]))
        ratios.append(page["grid"][k] / ref if ref > 0 else None)
    valid = [r for r in ratios if r is not None]
    if not valid or max(valid) <= 0 or min(valid) / max(valid) > UNEVEN_RATIO:
        return None
    cols = [np.mean([ratios[r * GRID + c] for r in range(GRID) if ratios[r * GRID + c] is not None])
            for c in range(GRID)]
    rows = [np.mean([ratios[r * GRID + c] for c in range(GRID) if ratios[r * GRID + c] is not None])
            for r in range(GRID)]
    if max(cols) - min(cols) >= max(rows) - min(rows):
        where = ("left", "right") if cols[0] < cols[-1] else ("right", "left")
    else:
        where = ("top", "bottom") if rows[0] < rows[-1] else ("bottom", "top")
    return (f"softness uneven across the page (softest at {where[0]}, sharpest at {where[1]}) "
            f"- possible lift or tilt rather than focus")


def manuscript_level(pages):
    content = [p for p in pages if p["group"] == "content" and p["score"] is not None]
    if len(content) < 6:
        return None, {}
    med = float(np.median([p["score"] for p in content]))
    bvals = [p["brightness"] for p in content if p["brightness"] is not None]
    mb = float(np.median(bvals)) if bvals else 0.0
    ivals = [p.get("ink") for p in content if p.get("ink") is not None]
    mi = float(np.median(ivals)) if ivals else None

    notes, usable = {}, []
    for p in sorted(content, key=lambda p: p["seq"]):
        if is_blank(p["score"], med, p["brightness"], mb, p.get("ink"), mi):
            notes[p["id"]] = "likely blank page (very low score at paper-level brightness) - not a focus call"
        else:
            usable.append(p)

    pat = side_pattern([(p["seq"], p["score"], p["pos"]) for p in usable])
    side_of = {p["id"]: _side_of(p["pos"], p["seq"])[0] for p in usable}
    if pat["basis"].startswith("sequence"):
        side_of = {p["id"]: ("odd" if p["seq"] % 2 else "even") for p in usable}

    for p in usable:
        if not p["local_flag"]:
            continue
        side, lr = side_of[p["id"]], p.get("local_ratio")
        expected = None
        if pat["verdict"] in ("systematic", "mild") and side == pat["softer_side"]:
            expected = pat["severity"]
        elif pat["verdict"] in ("drift", "localized"):
            for a, b, wr, _n in pat["windows"]:
                if a <= p["seq"] <= b:
                    wsoft = pat["side_a"] if wr < 1 else pat["side_b"]
                    wsev = wr if wr < 1 else 1 / wr
                    if side == wsoft and wsev < SIDE_MILD:
                        expected = wsev
                    break
        if expected is not None:
            if lr is None or lr >= PATTERN_EXPLAINS * expected:
                notes[p["id"]] = "one instance of the manuscript-level side pattern"
                continue
            notes[p["id"]] = (f"much softer than the side pattern ({lr:.2f} of neighbours, "
                              f"pattern {expected:.2f})")
        lift = _uneven(p, [q for q in usable if side_of[q["id"]] == side])
        if lift:
            notes[p["id"]] = f"{notes[p['id']]}; {lift}" if p["id"] in notes else lift
    return pat, notes

if __name__ == "__main__":
    main()
