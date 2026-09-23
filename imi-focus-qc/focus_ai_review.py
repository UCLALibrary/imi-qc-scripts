"""Optional vision-model review of ambiguous pages from either tool's focus report. See README.md."""
import argparse
import base64
import csv
import io
import json
import time
import os
import sys
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None

DEFAULT_CROP = 768

PROMPT = """You are helping quality-control a digitization project for early Islamic manuscripts.

You are shown two crops taken at native resolution from the SAME manuscript, captured in the same session:
  IMAGE 1: the page under review
  IMAGE 2: a neighbouring page, used as a reference for what this manuscript's captures look like when correct

Decide whether IMAGE 1 shows an OPTICAL FOCUS PROBLEM (a capture fault that would justify re-shooting) or whether it is acceptable.

Critical distinction. These look similar but are not the same thing:
- OPTICAL DEFOCUS: everything softens together. Ink edges AND the surrounding parchment/paper grain both lose definition at the same rate. This is a capture fault.
- INK BLEED / DEGRADED MATERIAL: only the ink boundaries are soft. The parchment grain immediately beside the ink stays crisp and high-contrast. This is the state of the object, not a capture fault.
- SPARSE OR FAINT WRITING: little ink, or pale ink, but what is there has clean edges. Not a capture fault.

So the key thing to examine is the un-inked substrate right next to the writing. If the grain and surface texture there are sharp in IMAGE 2 but mushy in IMAGE 1, that indicates defocus. If the grain is equally sharp in both and only the ink differs, that indicates material condition.

Respond with ONLY a JSON object, no other text:
{"verdict": "soft" | "ok" | "uncertain",
 "confidence": "high" | "medium" | "low",
 "cause": "optical_defocus" | "ink_bleed" | "sparse_or_faint" | "unclear",
 "reason": "<one sentence, max 25 words>"}

Use "uncertain" honestly when the two crops are genuinely close. A wrong confident answer is worse than an admitted uncertainty."""


def item_of(r):
    return r.get("folder") or r.get("manuscript") or ""


def order_of(r):
    try:
        return (0, int(r.get("sequence") or ""))
    except ValueError:
        return (1, r.get("file", ""))


def crop_b64(row, crop_px, cache_dir, jpeg_quality=92):
    if row.get("iiif_url"):
        from focus_check_iiif import fetch
        w, h = int(row["width"]), int(row["height"])
        cw, ch = min(crop_px, w), min(crop_px, h)
        url = f"{row['iiif_url']}/{(w - cw) // 2},{(h - ch) // 2},{cw},{ch}/full/0/default.jpg"
        raw = fetch(url, cache_dir)
        return base64.b64encode(raw).decode("ascii"), len(raw)
    return centre_crop_b64(Path(row["path"]), crop_px, jpeg_quality)


EXPLAINED = ("one instance", "likely blank", "fragment", "insert")


def centre_crop_b64(path: Path, crop_px: int, jpeg_quality: int = 92):
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        cw, ch = min(crop_px, w), min(crop_px, h)
        left, top = (w - cw) // 2, (h - ch) // 2
        crop = im.crop((left, top, left + cw, top + ch))
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=jpeg_quality)
    raw = buf.getvalue()
    return base64.b64encode(raw).decode("ascii"), len(raw)


def backend_stub(prompt, images_b64, model=None):
    return json.dumps({
        "verdict": "uncertain", "confidence": "low", "cause": "unclear",
        "reason": "stub backend - no model was called",
    })


def backend_gemini(prompt, images_b64, model="gemini-2.5-flash"):
    import urllib.request
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set in environment")
    parts = [{"text": prompt}]
    for b64 in images_b64:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
    body = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 300},
    }).encode()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["candidates"][0]["content"]["parts"][0]["text"]


def backend_claude(prompt, images_b64, model="claude-sonnet-5"):
    import urllib.request
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
    content = []
    for b64 in images_b64:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": b64}})
    content.append({"type": "text", "text": prompt})
    body = json.dumps({
        "model": model, "max_tokens": 300, "temperature": 0,
        "messages": [{"role": "user", "content": content}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return "".join(b.get("text", "") for b in data["content"])


BACKENDS = {"stub": backend_stub, "gemini": backend_gemini, "claude": backend_claude}


class RateLimiter:
    def __init__(self, rpm):
        self.min_interval = 60.0 / rpm if rpm and rpm > 0 else 0.0
        self.last = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self.last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last = time.monotonic()


def call_with_retry(fn, prompt, images, kwargs, retries, limiter):
    delay = 5.0
    for attempt in range(retries + 1):
        limiter.wait()
        try:
            return fn(prompt, images, **kwargs), None
        except Exception as e:
            msg = str(e)
            transient = any(t in msg for t in ("429", "500", "502", "503", "504",
                                                "timed out", "timeout"))
            if attempt < retries and transient:
                time.sleep(delay)
                delay *= 2
                continue
            return None, msg
    return None, "retries exhausted"


def parse_reply(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t.lstrip("`")
        t = t[4:] if t.lower().startswith("json") else t
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e == -1:
        return {"verdict": "parse_error", "confidence": "", "cause": "",
                "reason": t[:120]}
    try:
        return json.loads(t[s:e + 1])
    except Exception as ex:
        return {"verdict": "parse_error", "confidence": "", "cause": "",
                "reason": f"{ex}"[:120]}


def load_report(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def pick_candidates(rows, lo, hi, include_flagged):
    out = []
    for r in rows:
        if r.get("group") != "content" or r.get("error"):
            continue
        if (r.get("page_note") or "").startswith(EXPLAINED):
            continue
        lr = r.get("local_ratio")
        if not lr:
            continue
        try:
            lr = float(lr)
        except ValueError:
            continue
        in_band = lo <= lr <= hi
        already = r.get("flagged_possibly_out_of_focus") == "YES"
        if in_band or (include_flagged and already):
            out.append((r, lr))
    return out


def find_reference(rows, target_row):
    same = [r for r in rows
            if item_of(r) == item_of(target_row) and r.get("group") == "content"
            and r.get("sharpness_score")]
    same.sort(key=order_of)
    names = [r["file"] for r in same]
    try:
        i = names.index(target_row["file"])
    except ValueError:
        return None
    nbrs = [same[j] for j in (i - 1, i + 1) if 0 <= j < len(same)]
    if not nbrs:
        return None
    return max(nbrs, key=lambda r: float(r["sharpness_score"]))


def load_ground_truth(path):
    gt = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            v = (r.get("verdict") or "").strip().lower()
            if v in ("flagged", "blurry", "soft", "yes"):
                v = "soft"
            elif v in ("ok", "good", "no", "clear"):
                v = "ok"
            else:
                continue
            gt[Path((r.get("file") or "").strip()).name] = v
    return gt


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report_csv", type=Path,
                     help="page report from manuscript_focus_check.py")
    ap.add_argument("--backend", choices=sorted(BACKENDS), default="stub", help="stub (no calls), gemini, or claude (default stub)")
    ap.add_argument("--model", default=None, help="override the backend's default model")
    ap.add_argument("--band-low", type=float, default=0.70,
                     help="lower edge of the ambiguous local-ratio band (default 0.70)")
    ap.add_argument("--band-high", type=float, default=0.85,
                     help="upper edge of the ambiguous local-ratio band (default 0.85)")
    ap.add_argument("--include-flagged", action="store_true",
                     help="also send pages already below --band-low")
    ap.add_argument("--crop", type=int, default=DEFAULT_CROP,
                     help="crop size in pixels (default 768)")
    ap.add_argument("--limit", type=int, default=None,
                     help="stop after N pages")
    ap.add_argument("--rpm", type=float, default=10.0,
                     help="requests per minute (default 10)")
    ap.add_argument("--retries", type=int, default=3,
                     help="retries on 429/5xx errors (default 3)")
    ap.add_argument("--max-per-manuscript", type=int, default=None,
                     help="cap pages sent per manuscript")
    ap.add_argument("--dry-run", action="store_true",
                     help="prepare crops and report counts without calling any API")
    ap.add_argument("--ground-truth", type=Path, default=None,
                     help="CSV of known verdicts (file, verdict) to score against")
    ap.add_argument("--output", type=Path, default=Path("ai_review.csv"), help="results CSV (default ai_review.csv)")
    ap.add_argument("--cache-dir", type=Path, default=Path(".iiif_cache"), help="image cache for IIIF reports (default .iiif_cache)")
    args = ap.parse_args()

    rows = load_report(args.report_csv)
    cands = pick_candidates(rows, args.band_low, args.band_high, args.include_flagged)
    if args.max_per_manuscript:
        seen, capped = {}, []
        for row, lr in cands:
            f = item_of(row)
            if seen.get(f, 0) >= args.max_per_manuscript:
                continue
            seen[f] = seen.get(f, 0) + 1
            capped.append((row, lr))
        cands = capped
    if args.limit:
        cands = cands[:args.limit]

    total_content = sum(1 for r in rows if r.get("group") == "content")
    print(f"{total_content} content pages in report; {len(cands)} selected "
          f"(band {args.band_low}-{args.band_high}"
          f"{', plus already-flagged' if args.include_flagged else ''}).")
    if not cands:
        print("Nothing to review.")
        return

    gt = load_ground_truth(args.ground_truth) if args.ground_truth else {}
    call = BACKENDS[args.backend]
    limiter = RateLimiter(args.rpm if args.backend != "stub" else 0)
    out_rows = []
    bytes_sent = 0

    for n, (row, lr) in enumerate(cands, 1):
        ref = find_reference(rows, row)
        if ref is None:
            print(f"  skip {row['file']}: no neighbour to compare against")
            continue
        try:
            b64_t, sz_t = crop_b64(row, args.crop, args.cache_dir)
            b64_r, sz_r = crop_b64(ref, args.crop, args.cache_dir)
        except Exception as e:
            print(f"  skip {row['file']}: crop failed ({e})")
            continue
        bytes_sent += sz_t + sz_r

        if args.dry_run:
            verdict = {"verdict": "(dry-run)", "confidence": "", "cause": "", "reason": ""}
        else:
            kwargs = {"model": args.model} if args.model else {}
            reply, err = call_with_retry(call, PROMPT, [b64_t, b64_r], kwargs,
                                          args.retries, limiter)
            verdict = (parse_reply(reply) if err is None else
                       {"verdict": "api_error", "confidence": "", "cause": "",
                        "reason": err[:150]})

        rec = {
            "folder": item_of(row), "file": row["file"],
            "local_ratio": lr, "reference_file": ref["file"],
            "ai_verdict": verdict.get("verdict", ""),
            "ai_confidence": verdict.get("confidence", ""),
            "ai_cause": verdict.get("cause", ""),
            "ai_reason": verdict.get("reason", ""),
            "ground_truth": gt.get(Path(row["file"]).name, ""),
        }
        out_rows.append(rec)
        if not args.dry_run:
            print(f"  [{n}/{len(cands)}] {row['file']} vs {ref['file']}: "
                  f"{rec['ai_verdict']} ({rec['ai_confidence']}, {rec['ai_cause']})")

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    print(f"\n{len(out_rows)} pairs prepared. ~{bytes_sent/1024/1024:.1f} MB of image data"
          f"{' (not sent -- dry run)' if args.dry_run else ' sent'}.")
    print(f"Crops: {args.crop}x{args.crop} native resolution, 2 per page reviewed.")
    if args.dry_run:
        est_min = len(out_rows) / args.rpm if args.rpm else 0
        print(f"Would use {len(out_rows)} requests against your daily quota; "
              f"~{est_min:.0f} min at {args.rpm:g} RPM.")
    print(f"Report: {args.output}")

    if gt and not args.dry_run:
        scored = [r for r in out_rows if r["ground_truth"] and
                  r["ai_verdict"] in ("soft", "ok")]
        if scored:
            agree = sum(1 for r in scored if r["ai_verdict"] == r["ground_truth"])
            tp = sum(1 for r in scored if r["ground_truth"] == "soft"
                     and r["ai_verdict"] == "soft")
            fn = sum(1 for r in scored if r["ground_truth"] == "soft"
                     and r["ai_verdict"] == "ok")
            fp = sum(1 for r in scored if r["ground_truth"] == "ok"
                     and r["ai_verdict"] == "soft")
            unc = sum(1 for r in out_rows if r["ai_verdict"] == "uncertain"
                      and r["ground_truth"])
            print(f"\nAgainst ground truth ({len(scored)} decided, {unc} answered "
                  f"'uncertain' and excluded):")
            print(f"  agreement: {agree}/{len(scored)} ({100*agree/len(scored):.0f}%)")
            print(f"  caught {tp} soft pages, missed {fn}, false alarms {fp}")
            print("  NOTE: 'uncertain' replies are excluded from the agreement rate, so "
                  "read that number alongside the uncertain count, not on its own.")


if __name__ == "__main__":
    main()
