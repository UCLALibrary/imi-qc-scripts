# manifest_qc_v2.py

Filename QC for Islamic Manuscripts Initiative. Validates TIFF filenames against the project naming conventions, reporting structural anomalies and errors for review.

## Usage

```
python manifest_qc_v2.py MANIFEST.xlsx [MANIFEST2.xlsx ...] [--status status.csv] [--out PATH]
```

- Accepts `.xlsx` (reads whichever sheet has the most `.tif`-suffixed names
  in its first column — tab order varies between deliveries) or `.txt`/`.csv`
  (first column, one filename per line).
- `--status status.csv` — a CSV of `item,status` where status is `foliated`,
  `unfoliated`, or `paginated`. Items absent from it get every check except
  `check_status`, which depends on knowing which numbering scheme was
  expected.
- `--out PATH` — overrides where the combined CSV is written (see Output).
  The path is used exactly as given, so a bare filename lands in the current
  directory rather than `output/`.

## Output

Findings always print to stdout grouped by item. The same findings are also
written to one combined CSV (`manifest, item, reference, severity, code,
message`) across all input manifests:

- Default location: `output/` beside the script, named
  `<manifest-stem>-report.csv` for a single manifest or
  `manifests-report.csv` when several are given. The folder is created if
  missing, and a rerun on the same manifest overwrites its previous report.
- `output/` is gitignored; the location doesn't depend on the directory the
  script is run from.
- Written as UTF-8 with no BOM, LF line endings, and all fields quoted

## Filename structure

```
uclalsc_{collection}_ms{item}_{seq}[_{position}].tif
```

- `collection` + `item` together are the primary identifier for a
  manuscript. Item numbers are only unique **within** a collection — a
  manifest spanning multiple collections can reuse the same item number for
  two unrelated manuscripts — so the script groups and reports findings by
  `collection_item`, never by item number alone.
- `seq` is a continuous running count across the whole item, starting at
  0001, regardless of segment.
- `position` is optional and, where present, is `{segment}_{number}[side]`.

## Segments

| Segment | Meaning | Format |
|---|---|---|
| `a` | Front covers | `a_01` (exterior), `a_02` (inside) |
| `b` | Front flyleaves | `b_1r`, `b_1v`, `b_2r` ... |
| `f` | Folios | `f_1r`, `f_1v` ... |
| `y` | End flyleaves | `y_1r`, `y_1v` ... |
| `z` | Back covers / exterior details | `z_01` inside back cover, `z_02` back cover, `z_03` spine, `z_04` fore edge, `z_05` head, `z_06` tail, `z_07`+ extras |
| `frag` | Fragments / inserts | `frag1r` or `frag_1r` |
| `p` | Pagination (paginated items only) | `p_01`, `p_02` ... |

**`a`/`z` are fixed semantic slots, not a running count.** `z_01`–`z_06`
each mean something specific; an absent slot means that surface wasn't
photographed, which is reviewable rather than automatically wrong — that's
why `slot-absent` is a REVIEW finding, not an ERROR.

**`frag`'s underscore is optional** (`frag1r` and `frag_1r` are both valid)
as long as one style is used consistently within a single manuscript
(`check_frag_style`). What's never allowed, in either style, is a segment
letter glued on in front — `f_frag1r`, `b_frag2v` — because that isn't a
delimiter choice, it's a different segment mistakenly prefixed onto `frag`
(`check_grammar`'s `frag-prefixed` finding, via `PREFIXED_FRAG_RE`).

**`frag` is orthogonal to foliation status.** Fragments get coded whenever
present, regardless of whether the host item is foliated. An otherwise-bare
item with only frag-coded inserts is not a partial-foliation violation —
`check_mixed_naming` excludes `frag` from its "coded" count accordingly, and
`check_inserts` flags the fragments themselves for placement review.

## Two-slot parsing model

Each filename's position is read as two independent slots:

- the **segment** slot (`a`/`b`/`f`/`y`/`z`/`frag`/`p`) is checked against
  the whitelist above;
- the **position** slot (leaf number + `r`/`v`, or a bare counter) drives
  the ordering walk and the recto/verso completeness check.

When a segment letter is invalid (e.g. `v`, `q`), the position is still read
by its shape and, if it looks like a leaf (`number` + `r`/`v`), folded into
the folio (`f`) run — folios being where a stray segment letter almost
always lands. That keeps a segment typo and a genuine
ordering/completeness problem as two separate findings instead of one
masking the other.

## Checks

| Function | Severity | What it flags |
|---|---|---|
| `check_parsing` | ERROR | filename doesn't fit the overall pattern at all |
| `check_grammar` | ERROR | invalid segment, missing underscore, missing r/v side, or a segment letter glued onto `frag` |
| `check_sequence` | ERROR | `seq` doesn't start at 0001, has duplicates, or has gaps |
| `check_duplicate_tokens` | ERROR | the same position token (e.g. `f_3r`) used by more than one file |
| `check_segment_order` | ERROR | segments appear out of the expected a→b→f/p→y→z order |
| `check_folio_order` (`rv-order`, `leaf-order`, `leaf-gap`) | ERROR / REVIEW | recto/verso ordering inconsistent within a leaf; leaf numbers run backward (ERROR); leaf numbers skip (REVIEW — confirm against the ms's own foliation) |
| `check_inserts` | NOTE | a `frag` run is present — informational, review placement |
| `check_counters` (`counter`, `slot-absent`) | ERROR / REVIEW | `p` pagination isn't contiguous from 1 (ERROR); an `a`/`z` slot is skipped between the lowest and highest present (REVIEW) |
| `check_mixed_naming` | ERROR | some files bare, some coded, within the same item — foliation must be all-or-nothing across the a/b/f/y/z/p system (`frag` excluded, see above) |
| `check_one_sided` | REVIEW | a leaf has only recto or only verso named, not both |
| `check_padding` | NOTE | `a`/`z` positions mix zero-padding widths within an item |
| `check_flyleaf_count` | REVIEW | more than `FLYLEAF_REVIEW_THRESHOLD` (4) distinct `b`/`y` leaves — often means text-bearing preliminary/end leaves got folded into the flyleaf series rather than kept distinct; not a naming violation on its own |
| `check_frag_style` | ERROR | an item mixes `frag_1r`-style and `frag1r`-style within itself |
| `check_status` | ERROR | (only when `--status` supplied) files don't match their declared foliated/unfoliated/paginated status |

## Known limitations

- **Total absence of a segment isn't flagged.** `check_counters`'
  `slot-absent` only fires once at least one `a` or `z` file exists — a gap
  *between* present numbers. An item with **zero** `a` or `z` files at all
  (e.g. a genuinely unbound item, or a cover that never got photographed)
  produces no finding either way. Confirmed while reviewing Upload 3
  (2026-09): several items had no `a` segment whatsoever and weren't
  flagged. Candidate fix: add a check for zero `a`/`z` files, same REVIEW
  severity as `slot-absent` — not yet implemented, pending a decision on
  whether that's worth a standing check or just an eyeball step.
- The script validates naming only. It can't tell a genuinely misnamed
  file from a genuinely missing one, can't compare against a shot list's
  declared image count (that's a separate ad hoc check, not part of the
  script), and can't detect two different manuscripts' files sharing one
  item's name if both use internally-consistent, individually-valid
  filenames (that surfaces as row-count/shot-list mismatches instead —
  see the `decisions-and-learnings` log for the Upload 2 / Upload 3
  examples where this came up).
