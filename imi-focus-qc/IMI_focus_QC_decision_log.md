# IMI Automated Focus QC — Design Decision Log

**Tool:** `manuscript_focus_check.py`
**Purpose:** Triage large batches of digitized manuscript TIFFs to surface a short list of pages worth human review for focus problems.
**Status:** Validated against reviewer ground truth on the pilot batch; not yet run on a full production batch.

This log records what was decided, why, what evidence supported it, and — importantly — which decisions were later reversed when real data contradicted them.

---

## Framing decisions

### D1. Classical computer vision, not a trained model or paid API
**Decision:** Use variance-of-Laplacian (a standard edge-detection sharpness measure) computed locally. No machine learning, no per-image API calls.

**Why:** Out-of-focus detection is a solved classical CV problem. Running locally costs nothing per image and processes thousands of TIFFs in minutes. Routing every image through a paid vision model would cost real money per batch for a problem that doesn't need it.

**Status:** Held throughout. Nothing encountered since has challenged this.

---

### D2. Per-manuscript comparison, not a collection-wide threshold
**Decision:** Compare each page only against other pages within the same manuscript folder, never against a fixed global sharpness cutoff.

**Why:** Early manuscripts have inherent softness from ink bleed, paper texture, and age. That baseline varies enormously *between* manuscripts but is fairly consistent *within* one, since all its pages were shot in the same session. An absolute threshold would flag every page of a naturally soft manuscript and miss genuine problems in a sharp one.

**Evidence:** Synthetic testing showed a naturally soft manuscript's normal pages scoring ~5 while a sharp manuscript's normal pages scored ~587 — a single threshold cannot serve both.

**Status:** Held. This principle later drove the rejection of D8.

---

### D3. Robust statistics (median/MAD), not mean/standard deviation
**Decision:** Use median and Median Absolute Deviation with a modified z-score (Iglewicz & Hoaglin), rather than mean and standard deviation.

**Why:** A folder containing a few genuinely soft pages shouldn't have its baseline dragged around by them the way a plain average would.

**Status:** Held as a technique, though its *role* changed — see D9.

---

## Grouping decisions

### D4. Cover/binding shots compared separately, not excluded
**Decision:** Cover, spine, and exterior shots are scored and compared against each other in their own group, rather than dropped from analysis.

**Why:** Leather, board, and fabric produce very different baseline texture readings than paper, so comparing them against folios is meaningless. But excluding them entirely would mean a genuinely out-of-focus cover shot goes undetected.

**Revision:** An earlier version *excluded* them from analysis. Changed on the reasoning that focus problems in cover shots still matter.

---

### D5. Inside covers grouped with content, not with covers
**Decision:** Within segment `a`, only position 1 (exterior cover) goes to the cover group; positions 2+ (inside cover, pastedowns) go to content. Mirrored for segment `z`: position 1 (inside back cover) is content, positions 2+ are exterior.

**Why:** Inside covers are frequently paper or parchment bearing actual content — materially closer to a folio than to a binding.

**Evidence:** Real IIIF measurements confirmed this. In Ms 833/13, inside front/back covers measured saturation 0.18 — sitting with content pages (0.15–0.20), not with the covers (0.65–0.66).

---

### D6. Structural filename parsing, with positional fallback
**Decision:** Parse the segment code from the IMI naming convention (`uclalsc_{collection}_{item}_{seq}_{seg}_{pos}.tif`) rather than keyword-matching on words like "binding" or "cover". Auto-detect bare-sequence folders and fall back to positional head/tail identification via `--skip-first` / `--skip-last`.

**Why:** Reading the defined structure of the filename is robust to vendor phrasing. Keyword matching is not.

**Evidence:** The pilot CSVs confirmed this matters — only `boundmss-170` uses full segment-code naming; `aintabi-833`, `egyptianmss-955`, and `karubian-1183` are bare-sequential. Both modes are needed.

---

## Evidence-driven reversals

### D7. Color-distinctiveness demoted from primary method to secondary cross-check
**Original proposal:** Identify cover vs. content purely from pixels (brightness/saturation clustering per folder), removing the dependence on filenames entirely.

**What was tested:** Color statistics pulled from ~265 real images across all 10 pilot manuscripts via the IIIF server.

**Result:** 95% of true covers/spines/edges were correctly identified as color-distinct. But two failure modes appeared that threshold tuning cannot fix:
- **Illuminated pages dilute the signal.** In Ms 833/13, decorated content pages measured saturation 0.38–0.48 — closer to the cover (0.65) than to plain pages (0.15–0.20). This inflates the folder's spread enough to mask the actual cover.
- **Blank flyleaves are distinct in the opposite direction.** In Ms 170/904, four flyleaves (brightness 194–199, saturation 0.07–0.11) triggered deviations as strong as real covers — because they hold little ink, not because they're bindings.

**Decision:** Keep it as an optional `--color-check` that adds a `color_note` for manual review when a filename-classified content page is also color-distinct. It never reassigns groups or overrides sharpness flagging. A note means "these two signals disagree, look at it" — not a verdict.

---

### D8. Cross-folder systematic-blur check — built, then disabled by default
**Original reasoning (adopted from an external proposal):** Per-folder relative comparison has a real blind spot. If an *entire* manuscript is shot out of focus, every page is uniformly soft, nothing is an outlier, and the folder silently becomes its own bad normal. Comparing each folder's median against the batch median-of-medians should catch that.

**This was built and worked on synthetic data** — a deliberately blurred synthetic folder was correctly flagged at 0% of baseline while a merely-softer folder at 0.725× was correctly left alone.

**Then it was tested against real data and the reviewer's findings:**

| Manuscript | Reviewer verdict | Median sharpness |
|---|---|---|
| Ms 833/133 | all good | 898 |
| **Ms 955/141** | **uniformly slightly soft** | **699** |
| Ms 955/159 | all good | 591 |
| Ms 1183/5 | all good | 502 |

The one manuscript the reviewer identified as uniformly soft scored **higher** than two she cleared. The check misses it entirely and would instead falsely flag Ms 1183/5 — a sparse handwritten daftar whose low score reflects little ink per page, not poor focus.

**Why it fails:** Absolute Laplacian variance is dominated by script density, page size, and paper contrast — not focus. Comparing raw medians across different manuscripts compares the wrong thing. This is the same reasoning as D2, which we had already established and then briefly forgot when adopting an outside suggestion.

**Decision:** Default changed to `--folder-ratio-threshold 0` (disabled). The code and the per-folder summary CSV remain available for diagnostic use, but the check does not flag by default. Shipping a confidently wrong signal is worse than shipping no signal.

**Open problem:** The blind spot D8 was meant to close is real and remains unsolved. A uniformly out-of-focus manuscript will still pass. Any fix needs normalization for ink density before cross-manuscript comparison is meaningful.

---

### D9. Global outlier detection → local neighbour comparison (primary method change)
**This is the most significant change in the tool's design.**

**What prompted it:** Reviewer QC notes (Molly, pilot batch) recorded a recurring pattern across three separate manuscripts: "left-hand pages softer than right-hand pages." In a right-to-left manuscript, the recto sits on the left when open — so this describes rectos being systematically softer than versos.

**Verification against real images (Ms 833/66, contiguous run):**

| Folio | recto | verso | ratio |
|---|---|---|---|
| f. 1 | 624 | 1279 | 0.49 |
| f. 2 | 690 | 1369 | 0.50 |
| f. 3 | 722 | 1411 | 0.51 |
| f. 4 | 685 | 1313 | 0.52 |
| f. 5 | 661 | 1365 | 0.48 |
| f. 6 | 754 | 1609 | 0.47 |
| f. 7 | 1323 | 1454 | 0.91 |
| f. 8–12 | — | — | 0.87–1.05 |

The reviewer's finding is real and her boundary is exact: the problem is f.1r–6r and stops precisely at f.7r.

**How the old method performed:** Sampling across the full manuscript (f.1 to f.187, 34 pages), the folder-wide MAD test caught **0 of 11** reviewer-flagged pages. Even f.5r — less than half the sharpness of its facing page — scored a modified z of only −2.47 against a −3.5 threshold.

**Two structural reasons, neither fixable by tuning:**
1. **Wrong comparison.** The reviewer flips between facing pages; the tool compared to a folder-wide median. Ms 833/66 f.47r scores 765 against a folder median of 819 — entirely ordinary *for the manuscript*, while being 37% softer than its own facing page. A global comparison cannot see this by construction.
2. **MAD cannot flag a systematic subgroup.** When a large run of pages is soft together, they are not outliers — they are a substantial slice of the distribution, and they inflate the very MAD meant to detect them.

**Metric selection (chosen empirically, not by intuition):**

| Window / aggregate | Reviewer-flagged pages | All other pages |
|---|---|---|
| **±1, median** | **0.49–0.52** | **0.86–2.01** |
| ±1, max | 0.47–0.51 | 0.81–1.96 |
| ±2, median | 0.56–0.70 | 0.85–1.85 |
| ±3, median | 0.49–0.56 | 0.85–2.00 |

Immediate neighbours with a median gives the cleanest separation and an empty gap between 0.52 and 0.86. Wider windows measurably dilute the signal, because adjacent soft pages drag the reference down with them.

**Decision:** Local neighbour comparison (`--local-window 1`, `--local-ratio-threshold 0.70`) is now the primary detection signal. The folder-wide MAD test is retained as a secondary signal, since it can still catch an isolated catastrophic capture; the `flag_reason` column records which test fired.

**Validation result:** 6/6 reviewer-flagged pages caught, 0 false positives, across the flagged manuscript and a clean control (Ms 833/18).

---

## Human-in-the-loop decisions

### D10. Spot-check sheets close the D8 blind spot with a human absolute judgement
**Problem:** Every comparison the tool makes is relative. A manuscript captured uniformly out of focus has no internal outliers and passes silently. D8 was the attempted automated fix and it failed (see above). The blind spot remained open.

**Decision:** `--spot-check DIR` writes one contact sheet per manuscript showing pages the tool considers **ordinary** — chosen at the median local ratio, not from the flagged set. The reviewer looks at these; if they appear soft, the whole item is suspect regardless of what the per-page flags say.

**Why this works where automation didn't:** The tool answers "is this page worse than its neighbours." A human eye answers "is this acceptable at all." Those are genuinely complementary, and the second is precisely the absolute reference a relative method cannot supply.

**Why the tool picks the pages, not the reviewer:** Left to choose, a reviewer tends to open the first folio or an already-flagged page — neither is representative of the item. Selecting at the median local ratio makes the check a deliberate control rather than an ad hoc glance.

**Crops are native resolution and never downsampled.** The distinction between an acceptable and a soft capture lives in fine stroke edges and parchment grain; downsampling a full page destroys exactly that signal. This is why whole-page thumbnails cannot support this judgement.

**Validation:** On a synthetic batch containing one uniformly blurred manuscript, that manuscript's spot-check sheet showed pages with `local_ratio` ≈ 1.00 — flagged by nothing — that are visibly and unmistakably blurred. The sheet makes the failure obvious in one glance.

---

### D11. Vision-model assistance is viable, but only on native-resolution crops
**Question:** Can an AI vision model enhance this process without becoming a per-image cost on the whole corpus?

**Key constraint identified:** The reviewer's judgments are made "when zoomed all the way in," meaning the signal lives in fine detail at native resolution. Any image sent to a vision model is downsampled — which destroys precisely that evidence. Sending whole page images and asking "is this in focus" would likely underperform, because the model would be judging a version of the image where the signal has already been removed.

**Test performed:** Native-resolution 700×700 centre crops of Ms 833/66 f.5r (reviewer-flagged, local ratio 0.49) and its facing f.5v (sharp), pulled via IIIF region requests and inspected visually.

**Result:** The difference is clearly legible. The verso shows crisp ink-to-parchment transitions and visible fine parchment grain; the recto's strokes bleed slightly into the parchment and the grain is mushier. Same leaf, same capture session.

*Caveat:* the two crops rendered at different display sizes during inspection (1456px vs 1165px wide), which affects apparent sharpness independently of the image. Treat this as "the signal is present and assessable at crop resolution," not as a controlled measurement.

**Decision:** Architecture confirmed — send small native-resolution crops, never downsampled full pages. The spot-check sheets built in D10 already produce exactly this format, so they double as the input path if this step is later automated.

**Where a model would earn its cost (proposed, not yet built):**
- **The ambiguous band.** Local ratios roughly 0.70–0.85 are where thresholds don't separate cleanly and where the reviewer herself hedges. A small slice of pages, and exactly where judgement beats arithmetic.
- **Ink bleed vs. optical blur** (the deferred item). A model assessing whether parchment grain beside the ink is still sharp may be far cheaper than building edge-profile analysis from scratch.
- **The ink-density confound** (limitation 3). "Is this page soft, or just sparse?" is a categorical question a model should handle well.

**Next step before building:** run an accuracy test against known ground truth — send native-resolution crops of pages where the reviewer's answer is already known, and measure agreement. That yields a real accuracy number before any commitment.

---

### D12. Vision-model harness: Gemini, pair-based, ambiguous band only
**Vendor decision: Gemini.** Not on capability grounds — the specs disqualify neither, and no accuracy comparison has been run. The deciding factor is funding: Gemini is available through institutional/work access, while Claude API usage would be a personal out-of-pocket cost for a work project.

*Clarification recorded because it initially motivated the choice for the wrong reason:* a Claude.ai subscription and the Claude API are separate services with independent billing. API calls do not consume subscription chat limits. So "saving Claude tokens for other work" was not the real argument — the real argument is that a work project shouldn't be personally funded, which points to Gemini regardless.

**The backend is nonetheless kept vendor-swappable** (one function per vendor; `gemini`, `claude`, and `stub` implemented). Not because a switch is expected, but so that a second opinion on the ambiguous band, or a future institutional Claude agreement, costs a flag rather than a rewrite.

**Pairs, not single images.** Each request sends two native-resolution crops: the page under review and its sharpest immediate neighbour. Rationale:
- It mirrors how the human reviewer actually works (flipping between facing pages) and how `local_ratio` is computed.
- It gives a within-manuscript reference, so the model isn't judging softness against an imagined ideal.
- It blunts the ink-density confound (limitation 3): a sparse page is sparse in *both* frames, so a difference in edge quality is more likely to be capture-related.

**Ambiguous band only, not the whole corpus.** Default selection is `local_ratio` between 0.70 and 0.85 — the region where thresholds don't separate cleanly and where the reviewer herself hedged. Pages far below are already convincingly soft; pages above are already convincingly fine. Paying a model to re-confirm either is waste.

**768px crops.** Gemini tokenizes images into 768×768 tiles at 258 tokens each, so a 768px crop costs exactly one tile and anything smaller pays the same price for fewer pixels. The `--spot-check-crop` default in the main script was changed from 700 to 768 to match, so D10's spot-check crops double as vision-model input at no extra token cost. (For Claude, which tokenizes in 28×28 patches, multiples of 28 avoid waste instead.)

**The prompt targets the ink-bleed distinction directly.** It instructs the model to examine the un-inked substrate beside the writing — sharp grain in the reference but mushy in the target indicates defocus; equally sharp grain with only the ink differing indicates material condition. This is the deferred item from the external-proposal review, attempted via model judgement rather than by building edge-profile analysis.

**"Uncertain" is an allowed answer** and is excluded from the agreement rate but reported separately, so a model hedging on everything cannot post a misleadingly high score.

**Validation harness built before any spend.** `--ground-truth` scores model verdicts against the reviewer's own findings; `--dry-run` prepares crops and reports data volume with no API calls. Both the pipeline and the scoring arithmetic were verified with a stub backend.

**Status: not yet run against a real model.** The accuracy number that would justify relying on this does not exist yet. Run the ground-truth scoring first.

---

### D13. First prospective batch (Wellcome b1, 4 mss, 773 images) — the per-page flag list is the wrong unit
**Context:** First run on unreviewed material, checked post-publication via IIIF because pulling masters over the remote connection was impractical. Scored in the browser (the container cannot reach the image server). Results locked in `wellcome_b1_focus_predictions.csv` *before* reviewer QC, so this batch serves as a prospective test.

**Cover identification without titles or segment codes.** All four items are wholly unfoliated (`Image N`, bare-sequence filenames). Covers were still identifiable from pixels and dimensions alone, consistently across all four:
- **Exterior details by aspect ratio.** The last four images of every item are two tall strips (spine, fore edge) and two wide strips (head, tail); aspect ratio > 2.5 identifies them unambiguously.
- **Covers by brightness.** Contiguous runs at either end below 0.6× the item's median brightness.
- In two items, image 2 (inside cover) was dark — not paper — so D5's positional rule would have misfiled it. Brightness classification handled it correctly.

**Main finding: one side of the opening consistently softer, at manuscript scale.** The local ratio flagged ~half the pages in two items, almost all odd-numbered. Quantified as odd ÷ even sharpness:

| Item | Whole page | Central text block only | Pairs below 0.8 |
|---|---|---|---|
| ms0216 | 0.66 | **0.58** | 100% |
| ms0094 | 0.69 | **0.59** | 96% |
| ms0005 | 0.88 | — | 26% (within normal range) |
| ms0134 | 1.02 overall, but **drifts** from ~1.2 early to ~0.6 in the final pairs | — | — |

**Why this is capture, not material:** measuring only the central text block (excluding page-edge stacks, gutter, and backdrop) made the pattern *stronger*, ruling out a framing artifact. Scribe, paper, and ruling are identical on both sides of each leaf, so content cannot explain one side scoring ~0.6 of the other across 200 openings. ms0134's drift — one side improving while the other degrades through the book — fits the page planes changing height in the cradle as leaves turn, with focus left fixed. This is the same defect the reviewer identified in the pilot ("left-hand pages softer"), here at whole-book scale.

**Implications for the tool (proposed, not yet built):**
1. **Report at manuscript level first.** A book-wide alternation produces 100+ page flags that are all one problem. The tool should compute odd/even ratio per item and in rolling windows (to catch drift), report the pattern once, and suppress the per-page flags it explains.
2. **Measure the central text block by default.** The page-edge stack is a real confound: a blank page (ms0094 seq 88) scored 1034 because of edge stacks in frame. This is the deferred ROI-cropping item, now with evidence.
3. **Classify blank pages.** Seven pages in ms0005 and one verified in ms0094 scored under 15% of their item's median with paper-level brightness — blank leaves, not focus problems.
4. **Adopt aspect-ratio + brightness cover detection** for bare-sequence items.

**AI-assist result (Claude, visual, via IIIF 1:1 crops):** on this defect, the statistics were stronger evidence than visual inspection. Individual pairs showed visible but mild softening (moderate confidence on ms0216, moderate-low on ms0094), with content confounds — ruling lines, ink density — competing with the signal in any single comparison. Across 200 pairs those confounds average out; in one pair they don't. Visual checking was decisive only for the categorical question: ms0094 seq 89 is a blank ruled page, a clear false positive. This fits D11's expectation that a model earns its keep on categorical questions (blank vs. soft) rather than fine focus grading.

**Awaiting:** reviewer QC on this batch, conducted independently of the prediction file, to score it.

---

### D14. Manuscript-level reporting built; blank detection corrected; IIIF path confirmed as standalone
**Reviewer confirmation:** visual check of the Wellcome b1 items found rectos generally a bit fuzzier than versos, possibly also in ms0005 to a lesser degree — matching the measured patterns (0.58–0.59 systematic, ms0005 0.84 mild) and the pilot's "left-hand pages softer."

**What was built (both tools, shared code so they cannot drift apart):**
- **Side-pattern analysis per manuscript**, reported before any page flags. Each page on one side of the opening is compared with its immediate neighbours on the other side; recto/verso is taken from filenames where encoded, sequence parity otherwise. Verdicts: systematic / mild / drift / localized / none, with rolling windows showing where it is strongest.
- **Page flags annotated, not suppressed.** A flag that is one instance of the pattern says so in `page_note`; flags with an empty note are the ones needing individual review. On the synthetic tests, every soft-page flag resolved to the pattern.
- **Central text-block measurement by default** (`--roi central`; `full` available), per D13's evidence.
- **IIIF tool:** cover/exterior classification from titles when informative, otherwise aspect ratio (strips > 2.5) and dark end-runs; requests capped at native region width, fixing the ten narrow-strip failures.

**Validation against real data** (the Wellcome b1 scores): ms0216 and ms0094 systematic (0.66, 0.70 on whole-page scores), ms0005 mild (0.84), ms0134 drift. The pilot 833/66 run reads correctly with its localized f.1r–6r stretch visible in the windows (0.51 → 0.98).

*Calibration caveat:* the drift rule (windows crossing 1.0 with a swing ≥ 1.3×) was first set to require a window below 0.80, which **missed** ms0134. It was changed to test the swing, which the cradle-height mechanism predicts. That change was made against a single example and should be treated as provisional until more items are seen.

**Correction — blank detection could hide the worst captures.** The first blank rule (very low sharpness at paper-level brightness) tagged 18 heavily blurred text pages in a synthetic test as "likely blank." A badly out-of-focus page and a blank page both score near zero, so sharpness cannot separate them, and the mistake would have filed the most serious failures under a harmless label. The rule now also requires low **ink deficit** (95th-percentile minus mean luminance): blur redistributes ink but does not remove it, so a blurred text page keeps its deficit while a blank page has almost none. Re-tested: exactly the one planted blank is tagged; all blurred pages remain flagged. If the ink measure is unavailable, the rule declines to call anything blank. *Note:* the corrected rule is validated on synthetic data only; the eight real blanks from Wellcome b1 were identified before the ink measure existed.

**IIIF path is standalone and sustainable.** The Wellcome b1 run exhausted a chat session because the scoring was done through the browser and results were relayed through chat in small chunks — the sandbox cannot reach the image server. That cost was an artifact of *where* it ran, not of IIIF. `focus_check_iiif.py` runs locally, needs no AI or API, caches every fetch (so an interrupted run resumes), and for ~800 images should take minutes. The AI assist is optional and, per D13, most useful for categorical "blank or soft?" calls.

**Not yet done:** pixel-based cover classification in the TIFF tool (requires scoring before classifying, a larger restructure); network path of the IIIF tool has not been exercised outside the browser.

---

### D15. First standalone run (Wellcome b1, on the reviewer's Mac) — two bugs, one retraction
**Run:** `focus_check_iiif.py` against 773 IIIF images, locally, no AI or API. Completed in one pass; the IIIF fetch path worked first time outside the browser.

**Bug — a few fragment files switched a whole manuscript into recto/verso mode.** Two items contained fragment images named `…_frag1r` / `…_frag1v`. Those two to four files carried an r/v marker, so the side analysis concluded the item encoded recto/verso in filenames and paired only the fragments — reporting "too few paired pages" for a 424-image manuscript. Fixed: filename sides are used only when ≥80% of pages carry them; otherwise sequence parity. Fragments are now also excluded from side pairing and annotated, since they are often shot on a backing sheet and their scores are not comparable to pages.

**Retraction — ms0134's "drift" was a framing artifact.** D13 reported ms0134 drifting from one side sharper (~1.22) to the other (~0.89) through the book, and D14 adjusted the drift rule to detect it. Measured on the central text block, ms0134 is flat: 0.95–1.10 across the whole book, verdict *none*. The page-edge stack moves from one side of the frame to the other as a book is paged through, which would produce exactly that whole-page swing. So the D13 finding for ms0134 was the edge stack, not the cradle — and the drift rule was tuned to an artifact. The rule remains in place but has **no validated real example**. This is further evidence for central-region measurement (D13, D14), and a reminder that one ambiguous case is not calibration.

**Blank rule recalibrated against real data.** The ink-deficit rule from D14 (validated on synthetic data only) missed six blank ruled openings in ms0094, including seq 88/89, which had been confirmed blank visually. Their ruling lines keep sharpness at 0.2–0.3× the median, above the old 0.15 cutoff, and ruling plus paper grain puts their ink at 0.42–0.60× the item median, above the old 0.35. Recalibrated to sharpness < 0.50× and ink < 0.65× (with paper-level brightness). On this batch that tags every known blank; the thinnest margin is in ms0005, where a tagged page sits at 0.49 and the nearest untagged at 0.64. Calibrated on one batch — provisional. Tags annotate flags; they never remove them.

**Result on real data:**

| Item | Side pattern | Page flags → pattern / blank / fragment / **review** |
|---|---|---|
| ms0094 | systematic, odd 0.58× | 188 → 177 / 6 / 2 / **3** |
| ms0216 | systematic, odd 0.58× | 57 → 56 / 0 / 0 / **1** |
| ms0005 | systematic, odd 0.78× | 19 → 10 / 8 / 1 / **0** |
| ms0134 | none (0.97×) | 4 → 0 / 0 / 2 / **2** |

268 page flags reduce to three manuscript-level findings and six pages for individual review — all six are the first or last content page beside a cover (flyleaves or pastedowns on a different paper tone). ms0005 moved from *mild* (0.84, whole page) to *systematic* (0.78, central block), consistent with the reviewer seeing a smaller but real recto/verso difference there. Browser-based (D13, central block) and standalone runs agree on ms0094 and ms0216 at 0.58–0.59.

---

### D16. Fragments and inserts are different things, and neither belongs in the page comparison
**Correction from the reviewer:** most images coded `frag` in Wellcome b1 are not fragments but *inserts* — bookmarks and loose notes. And the distinction matters: a true fragment is part of the text block; an insert is a foreign object photographed in sequence. D15's note described both as "a separate object from the text block," which is wrong for fragments.

**Two separate questions, kept apart:**
- *Belonging* (a cataloguing question): fragment yes, insert no.
- *Comparability* (the focus question): neither photographs like the pages around it. Fragments are often on a backing sheet, damaged, small, or not lying flat; in this batch they scored anywhere from 0.002× to 1.6× the item median. Compared against neighbours, they are falsely flagged — and they distort their *neighbours'* local reference too.

**Decision (option 1 of three discussed):** fragments and inserts each get their own group, excluded from page flags, neighbour comparisons, and side pairing. **Fragments** are placed on the spot-check sheet as native-resolution crops, because excluding them from the comparison would otherwise mean nothing checks their focus — the opposite of what is wanted for material scholars often care about most. **Inserts** are annotated only; this is easy to change if they turn out to need a look.

*Alternatives not chosen:* comparing fragments only with each other (most items have one or two), or treating unbacked fragments as ordinary pages (would need backing-sheet detection, and one batch is thin evidence that unbacked fragments score comparably).

**Recognition:** from the title ("Fragment 1r", "Insert 2") or the filename's final token (`frag1r`, `insert2v`). The title takes precedence, so relabelling an insert by hand overrides a vendor's `frag` filename. Recognition happens *before* any cover or dimension rule.

**Two bugs found while building this:**
1. **Fragments misfiled as exterior shots.** ms0005's first fragment pair is ~1000×3600 px; the aspect-ratio rule (D14) filed them as spine/fore-edge. Fixed by recognizing fragments first.
2. **Fragments switched a bare-sequence folder into "segment-coded" mode in the TIFF tool** — the same class of bug D15 fixed in the side analysis. Because `frag` is a segment code, a bare-sequence item with two fragment files was treated as fully coded, which silently disabled `--skip-first/--skip-last` and put its cover among the pages. This would have affected ms0005 and ms0094 in the TIFF tool. Fixed: fragments and inserts don't count toward deciding an item's naming mode.

**Consequence for cataloguing, outside this tool:** the imaging spec defines only `frag`, and `label_titles.py` turns `frag` into "Fragment N". Inserts will be mislabelled as fragments in Titles unless relabelled by hand, given a rule in `label_titles.py`, or given their own code in the spec. For the focus tool, relabelling now changes routing — relabelled inserts leave the fragment spot-check sheet.

---

### D17. Pages worse than the pattern stay visible; lift/tilt diagnostic; AI review accepts IIIF reports; scripts cleaned
**Fix A — the side pattern no longer excuses pages far beyond it.** Previously every flagged page on the soft side of a pattern manuscript was labelled "one instance of the side pattern," so a genuinely bad capture that happened to be a recto looked explained away. Now a page is attributed to the pattern only if its local ratio is at least 0.70 × the pattern's severity; otherwise it's noted "much softer than the side pattern" and counts as needing review. Two ways of setting the expectation were compared on Wellcome b1: the whole-book pattern (chosen) and the pattern in that stretch of the book. The stretch-based version surfaced 2 pages; the whole-book version 7, including ms0094 seq 383–395 at 0.34–0.40 against a pattern of 0.58. Those are the worst rectos in the book, and the stretch-based version hid them precisely *because* the stretch is bad — the same "explained away" failure one level up.

**Lift/tilt diagnostic.** Test case supplied by the reviewer: Ms 170/904 f. 71v, a page that lifted during capture.
- *Detection:* the existing central-block measurement already catches it — 0.39 of other versos, local ratio 0.59 (below 0.70). It sits on the sharp side of a recto pattern (~0.63 in this manuscript, a third collection showing the pattern), so it was not absorbed by the pattern label.
- *Shape:* measured in a 3×3 grid against the median of other versos, the page shades from ~0.2 at left to ~0.6 at right within the central block (0.26–0.41 vs 0.83–0.92 on the whole page). A normal verso stays within 0.90–1.35 in every cell. Visually, f. 72v has a page weight on the left edge; f. 71v has none, and its left side is the soft side.
- *Built:* for flagged pages, each grid cell is compared with the median of the same cell on *same-side* pages; if the softest cell is under half the sharpest, the note reads "softness uneven across the page (softest at …) — possible lift or tilt rather than focus." Comparing within the same side matters: in a control where every evenly soft recto was force-flagged, none was labelled a lift.
- *Untested:* a lift confined to a corner or outer edge, outside the central block; any lift other than this one.

**"Needs review" redefined.** Previously a flag with an empty note. Now: a flag whose note is not one of the explaining ones (pattern, blank, fragment, insert). Same intent, but review-worthy flags can now carry a note saying why.

**AI review accepts IIIF reports.** It was written before the IIIF tool and read local file paths and a `folder` column. It now works from either report — pairing pages by sequence within each manuscript and fetching crops from IIIF where the report came from the IIIF tool (which now records image width and height for this). Pages already explained are no longer sent.

**Scripts cleaned.** At the reviewer's request, comments, docstrings and design notes were removed from the scripts, `--help` text reduced to short factual lines, and usage documentation moved to `README.md`. Rationale and history remain here. Two `--help` strings were wrong at the time (`--color-check` referred to a removed docstring; `--folder-ratio-threshold` claimed a default of 0.5 when it is 0).

**Result on Wellcome b1:** 13 pages needing review — the six beside covers, plus ms0005 seq 13 and ms0094 seq 7, 343, 383, 389, 393, 395 as "much softer than the side pattern."

---

### D18. TIFF tool brought in line with the IIIF tool; covers kept with the pages on uncoded items
**Why now.** The first real TIFF runs (Molly, thumb drive and NetApp) showed the TIFF tool had fallen behind: spot-check off by default, loose output files, no `summary.txt`, three spot-check pages chosen regardless of side.

**Two apparent bugs were the thumb drive, not the code.** On the drive, one manuscript produced 130 read errors and neither manuscript got a spot-check sheet; the same files on NetApp — copied *from* that drive — read without a single error. The errors were `[Errno 2] No such file or directory` for files the tool had just listed, and persisted with `--workers 1`, which rules out concurrency. That is the signature of the drive disconnecting mid-run (macOS may remount it under a new name, so the old path vanishes). The missing sheets followed from it: the pages picked for the sheets were among the unreadable ones. The tool now counts files that disappear after listing and prints a plain warning that the drive or share probably disconnected, instead of dozens of identical errors; the README suggests `caffeinate -m`, a direct port, and a different cable.

**Parity changes (TIFF tool):** results in `output/<collection-folder>/` with `report.csv`, `report-manuscripts.csv` (renamed from `<output>-folders.csv`), `summary.txt` and `spot/`, overwritten on rerun under the same guard as the IIIF tool; spot-check sheets by default (`--no-spot-check` to skip; `--spot-check` still accepted so older commands work); one ordinary page per side of the opening plus every fragment on the same sheet; the printed summary in the IIIF tool's per-manuscript format. The cross-folder check only prints when enabled.

**Cover handling on uncoded items (the hybrid).** Asking the vendor or reviewers to state cover counts per manuscript isn't realistic. Three approaches were weighed:
1. *Classify covers from pixels*, as the IIIF tool does (dark end-runs). Rejected for the TIFF tool: pilot bindings 833/13 (yellow fabric) and 833/18 (green cloth) measured ~0.7× page brightness, not dark enough for the rule — they were distinct by colour saturation instead, and a combined colour rule would rest on a handful of bindings.
2. *Don't classify covers at all; annotate the ends.* A cover left among the pages costs one or two noisy pairs in a side analysis of 30–200, and a spurious flag or two at the ends.
3. *Hybrid* — chosen: segment codes where present; exterior strips (spine, edges) set aside by shape, which needs only the image dimensions from the TIFF header; everything else treated as pages. Flags in the first or last three pages are noted "near the start/end of the item — may be a cover, pastedown or flyleaf," and still count as needing review, since a genuinely soft first page happens (ms0503 p_01 scored 0.18).

The reviewer's point that settled it: many manuscripts have no binding at all, so for them every image is a page, and any cover-guessing rule could only misfile one. The failure mode of the hybrid is a flag you dismiss, rather than a page silently treated as a cover and never checked.

**The IIIF tool is unchanged**, including its darkness-based cover rule; the near-the-ends note is off there. The two tools therefore group covers differently on uncoded items — a known, deliberate difference. `--skip-first` / `--skip-last` remain available in the TIFF tool as an optional override.

**First TIFF results (NetApp, uclalsc_1147):** ms0503 systematic, odd 0.69× (96% of 24 pairs); ms0893 mild, odd 0.82× (95% of 37 pairs) — the recto pattern in a fourth collection. ms0503's 19 blank-tagged pages form runs at both ends (blank flyleaves), consistent with the blank rule rather than a threshold misfire. These files use a `p` segment code for pages, which the tool doesn't list among its known codes; they fall through to the page group, which is correct, but sides then come from sequence parity rather than the filename. Confirmed with the project: `p` is the paginated counterpart of `f`, and it is now a recognized code handled the same way. The practical difference is that an item named only with `p` codes is read as segment-coded rather than bare-sequence; sides still come from sequence parity, since page numbers carry no recto/verso marker.

---

## Known limitations

1. **A uniformly out-of-focus manuscript can still pass automated detection.** Both the local and folder-wide comparisons are relative, so if everything is equally soft, nothing stands out. D8 was the attempted automated fix and it failed on real data. **Mitigated by D10** — the spot-check sheets put a human absolute judgement in the loop — but not solved automatically, and the mitigation depends on the reviewer actually looking at the sheets.
2. **Three or more consecutive soft pages can evade the local check**, since a page's neighbours are then also soft. The alternating recto/verso pattern is detected cleanly because soft pages are separated by sharp ones; a solid run is harder.
3. **Ink density confounds absolute scores.** Measured ink fraction across real pages ranged 0.10–0.23, and sparse pages score low regardless of focus. In Ms 1183/4, a flyleaf scored 174 — lower than the inside front cover at 339 that the reviewer *did* flag — without being a focus problem. Normalizing by ink coverage is unexplored and would likely improve cross-page comparability.
4. **Cover groups are statistically thin.** With only 2–10 covers per manuscript, MAD statistics are unreliable; `--cover-threshold` exists to compensate but reading the raw scores directly is often more trustworthy.
5. **Marginal cases are genuinely marginal.** In a clean control manuscript, recto/verso ratios ranged 0.74–1.48. The reviewer's own uncertain calls (ratios ~0.81–0.84) fall inside that range. No threshold cleanly separates them, and the reviewer herself flagged these as uncertain.
6. **Not yet run on a full production batch.** All validation to date is on the 10-manuscript pilot, using sampled pages rather than complete sequences.

---

## Deferred

**Ink-bleed vs. optical-blur discrimination.** Optical defocus softens everything uniformly, while ink bleed softens only ink boundaries — the surrounding parchment grain stays sharp. Distinguishing them via edge-profile analysis at full resolution is physically well-grounded and would reduce false positives on degraded material. Deferred until a full batch shows whether the false-positive review load justifies the effort; it requires reliable ink/parchment segmentation across wide material variation, which is substantially harder than anything built so far.

**Central-ROI cropping.** Cropping to the central portion of a page before measuring, to avoid gutter curvature and binding shadow skewing the score. Plausible, untested. Risk: cropping may exclude marginalia and seals that still need focus checking.

**Illumination/blank page sub-grouping.** Illuminated pages and blank flyleaves currently sit in the same `content` sharpness group as ordinary text pages. Whether this causes real false positives has not been measured on production data. Worth testing before adding complexity.

---

## Method note

Validation used the IIIF image server rather than the source TIFFs, since the originals were not directly accessible. Sharpness was computed on images at 1200–1500px wide, matching the tool's internal downsampling. Absolute values are therefore not comparable to a run against full-resolution TIFFs, but the *relative* comparisons — which is all the tool uses — hold.
