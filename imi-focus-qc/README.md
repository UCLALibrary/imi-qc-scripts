# IMI Focus QC

Finds out-of-focus captures in digitized manuscripts. It measures the sharpness of every image, compares each page with the pages around it, and reports problems at two levels: patterns that affect a whole manuscript (such as every recto being softer than every verso), and individual pages that stand out.

Everything runs locally. No AI service or API is needed.

All three scripts write to `output/` beside the scripts unless given an explicit path, and create it if missing. `output/` is gitignored, so results are never committed. CSVs are written as UTF-8 with no BOM, LF line endings, and all fields quoted.

## Files

| File | What it's for |
|---|---|
| `focus_check_iiif.py` | Checks images already on the IIIF server, from a Pages CSV export. |
| `manuscript_focus_check.py` | Checks TIFFs on disk, one folder per manuscript. Also holds the shared analysis code, so it must sit in the same folder as `focus_check_iiif.py`. |
| `focus_ai_review.py` | Optional. Sends borderline pages to a vision model for a second opinion. See [Optional: AI review](#optional-ai-review). |
| `IMI_focus_QC_decision_log.md` | Why the tool works the way it does, including the evidence and the changes along the way. |

## Setup

Requires Python 3.9 or later and three packages:

```
python -m pip install pillow numpy scipy
```

If that fails with "externally-managed-environment" (common with Homebrew Python), use a virtual environment:

```
python3 -m venv .venv
source .venv/bin/activate
pip install pillow numpy scipy
```

Run `source .venv/bin/activate` again in each new terminal.

## Quick start

**Images on the IIIF server:**

```
python focus_check_iiif.py path/to/wellcome-b1-1-pages.csv
```

Results go in a folder named after the CSV, inside `output/` beside the scripts — wherever you run the command from:

```
output/wellcome-b1-1-pages/
    report.csv
    report-manuscripts.csv
    summary.txt
    spot/
```

Several CSVs can be given in one command; each gets its own folder. Running the same CSV again replaces its folder. If that folder contains anything other than these results, the script stops rather than delete it.

`output/` is gitignored. `--output-dir` puts the batch folders somewhere else.

Downloaded images are cached in `.iiif_cache/`, so re-running (for example, after changing a setting) takes seconds, and an interrupted run picks up where it stopped.

**TIFFs on disk:**

```
python manuscript_focus_check.py path/to/collection --spot-check
```

Writes `output/focus_report.csv`, `output/focus_report-folders.csv`, and (with `--spot-check`) sheets in `output/spot/`. `--spot-check` takes an optional folder; without one it uses `output/spot`.

`path/to/collection` contains one subfolder per manuscript, with that manuscript's TIFFs directly inside.

## Input

**IIIF:** a Pages CSV with these columns: `Parent ARK`, `Item Sequence`, `Title`, `File Name`, `IIIF Access URL`, `media.width`, `media.height`. Each Parent ARK is treated as one manuscript. Several CSVs can be given at once.

**TIFF:** filenames following the IMI convention, either segment-coded (`uclalsc_0833_ms0010_0005_f_1r.tif`) or bare-sequence (`uclalsc_1148_ms0005_0012.tif`). For bare-sequence items, use `--skip-first` and `--skip-last` to say how many images at each end are covers and exterior shots.

## What it does

1. **Sorts images into groups** so like is compared with like:
   - *covers* and *exterior* shots (spine, fore edge, head, tail) — compared only with each other
   - *fragments* — left out of the comparison and put on the spot-check sheet for a direct look
   - *inserts* (bookmarks, loose notes) — left out and not checked
   - *content* — everything else; this is where focus is assessed

   The IIIF tool uses titles where they're informative ("Front cover", "Spine", "Fragment 1r", "Insert 2"). Where they aren't (e.g. "Image 12"), it identifies exterior shots by shape (long thin strips) and covers by being much darker than the pages. The TIFF tool uses segment codes, or `--skip-first` / `--skip-last` for bare-sequence items. A title of "Insert" overrides a `frag` filename.

2. **Measures sharpness** of the central text block of each image, leaving out the margins, gutter, and page-edge stacks, which would otherwise distort the score. Use `--roi full` to measure the whole image.

3. **Looks for a side pattern** in each manuscript: whether pages on one side of the opening are consistently softer than those on the other. Uses recto/verso from filenames where they're encoded, otherwise odd/even sequence numbers.

4. **Flags individual pages** that are much softer than their immediate neighbours (below 0.70 of them by default).

5. **Annotates flags.** Some have an ordinary explanation (part of the side pattern, a blank leaf). Others get a note saying why they're worth a look: much softer than the side pattern, or softness concentrated on one side of the page (a possible lift or tilt). Nothing is removed.

## Reading the results

Read in this order.

### 1. The manuscript summary

Printed at the end of the run. The IIIF tool saves the printed text as `summary.txt`, and the same figures as `report-manuscripts.csv`; the TIFF tool saves `<output>-folders.csv`. One line per manuscript, with a side-pattern verdict:

| Verdict | Meaning |
|---|---|
| `SYSTEMATIC` | One side of the opening is consistently softer, e.g. "odd softer: 0.58x the other side, 98% of pairs". Usually a cradle or focus setup problem affecting the whole capture. |
| `MILD` | The same, but a smaller difference. |
| `DRIFT` | Which side is softer changes partway through the book. |
| `LOCALIZED` | Present in part of the book only. |
| `NONE` | No side pattern. |

The line also shows the range across the book and the stretch where it's strongest, e.g. "strongest in seq 377–395".

### 2. The page report

`report.csv` (IIIF) or the `--output` file (TIFF), one row per image. The flags worth looking at are those with `flagged_possibly_out_of_focus = YES` whose `page_note` is empty or is one of the "look at this" notes below. The summary gives the count as "need individual review".

`page_note` values:

| Note | Meaning |
|---|---|
| *(empty)* | Needs looking at. |
| `much softer than the side pattern (…)` | On the soft side, but well beyond the pattern (below 0.70 of it). Needs looking at. |
| `softness uneven across the page (softest at …)` | Part of the page is much softer than the rest, compared with other pages on the same side. Suggests the page lifted or tilted rather than a focus error. Needs looking at. |
| `one instance of the manuscript-level side pattern` | This page is soft because of the side pattern; deal with the pattern rather than the page. |
| `likely blank page …` | Little or no writing, so a low score isn't a focus problem. |
| `fragment - …` | Not compared with pages; check it on the spot-check sheet. |
| `insert - …` | Not part of the text block; not checked. |

Other useful columns: `local_ratio` (sharpness as a fraction of the neighbours), `group`, `sharpness_score`.

### 3. The spot-check sheets

One image per manuscript in the `spot/` folder. The IIIF tool makes these by default; the TIFF tool makes them with `--spot-check <folder>` and puts fragments in a separate `FRAGMENTS` image. Each tile is a crop from the middle of a page at full resolution — the same as zooming to 100% in the viewer — and is labelled with what it is.

- **Ordinary odd page / ordinary even page** (IIIF tool): a typical page from each side, not a flagged one. If both are acceptable, that manuscript is acceptable apart from any individually flagged pages. If one isn't, the pages on that side generally aren't either. In a manuscript with a side pattern, expect the soft side to look softer; the question is whether it's still acceptable. The TIFF tool shows three typical pages rather than one per side.
- **Fragment – judge focus directly**: every fragment, since they aren't compared with anything.

This is the only check for a manuscript that is soft *throughout*: every comparison the tool makes is relative, so a uniformly soft book looks normal to it.

### Where each kind of problem shows up

| Problem | Where to find it |
|---|---|
| A few badly out-of-focus pages | Page report: flags needing review |
| A page lifted or tilted | Page report: "softness uneven across the page" |
| One side of the opening softer | Manuscript summary |
| Whole manuscript soft | Spot-check sheet only |
| Fragments | Spot-check sheet only |
| Blank leaves | Page report, noted as likely blank |

## Common options

| Option | Default | Effect |
|---|---|---|
| `--local-ratio-threshold` | 0.70 | Lower flags fewer pages; higher flags more. |
| `--roi` | `central` | `full` measures the whole image. |
| `--output-dir` | `output/` | IIIF: where batch folders are created. |
| `--output` | `output/…` | TIFF, AI review: report file. |
| `--no-spot-check` | — | IIIF: skip the spot-check sheets. |
| `--spot-check` | off | TIFF: folder for spot-check sheets. |
| `--spot-check-pages` | 2 (IIIF), 3 (TIFF) | Ordinary pages per sheet. |
| `--workers` | 6 (IIIF) | Simultaneous downloads. |
| `--cache-dir` | `output/.iiif_cache` | IIIF image cache, shared by both IIIF-based scripts. |
| `--skip-first`, `--skip-last` | 0 | TIFF, bare-sequence items: covers at each end. |

Run either script with `--help` for the full list.

## Known limitations

- **A manuscript soft throughout is not detected automatically.** Use the spot-check sheets.
- **A stretch where both sides are soft together** is mostly missed: each page is compared with neighbours that are just as soft. Usually only the first page of the stretch is flagged.
- **Softness confined to a corner or the outer edge of a page** may be missed, because only the central block is measured by default. The lift diagnostic has been checked against one real lifted page, where the soft area covered about two thirds of the page.
- **Odd/even sides assume one image per page side.** A single unpaired extra image (such as an additional flap shot) swaps odd and even from that point on.
- **The blank-page rule was calibrated on one batch**, and the drift rule has no confirmed real example yet.
- **Scores from IIIF images and from TIFFs are not comparable** with each other. Only compare within one run.

## Optional: AI review

`focus_ai_review.py` sends borderline pages (local ratio between 0.70 and 0.85 by default) to a vision model, as pairs: the page and its sharpest neighbour, cropped at full resolution. It needs an API key (`GEMINI_API_KEY` or `ANTHROPIC_API_KEY`).

```
python focus_ai_review.py wellcome-b1-1-pages/report.csv --backend stub --dry-run    # no calls; shows what would be sent
python focus_ai_review.py wellcome-b1-1-pages/report.csv --backend gemini --rpm 10   # real run
```

With `--ground-truth known.csv` (columns `file`, `verdict` = soft/ok) it scores the model's answers against known results.

It accepts reports from either tool; for IIIF reports it fetches the crops from the image server (cached in `--cache-dir`). Pages already explained (side pattern, blank, fragment, insert) are not sent. In testing so far, the side-pattern statistics were more reliable than visual review for systematic problems; the model was most useful for telling blank pages from soft ones.
