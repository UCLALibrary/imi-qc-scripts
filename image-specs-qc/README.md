# Image Spec Check

Checks technical specs in TIFFs — embedded color profile, bit depth,
channel count, compression, and resolution — against the imaging delivery
spec.

## What it checks

| Check | Flags when |
|---|---|
| Compression | Not in the allowed list (default: uncompressed only) |
| Bit depth | `BitsPerSample` not in the allowed set (default: 8-bit/channel) |
| Color mode | Not RGB (photometric interpretation ≠ RGB), or samples per pixel ≠ 3 |
| Color profile | No embedded ICC profile, or its description doesn't contain the expected text (default: `sRGB`) |
| Resolution | PPI not within tolerance of the standard or enhanced tier (default 400 / 600); x and y resolution mismatched |

## How it groups files

Any directory that directly contains `.tif` files is treated as one
manuscript. By default the first file in it (alphabetically) is checked;
pass `--sample N` to check more. This assumes every file in a manuscript's
directory was exported together with identical settings (e.g. one Lightroom
batch export), so one file stands in for the whole item.

Works at any depth — point it at a single ms folder or a whole batch folder
and it finds every ms directory underneath.

## Usage

```
python check_image_specs.py /Volumes/network-drive/batch01
python check_image_specs.py ROOT --compression none lzw --bit-depth 8 16
```

## Options

| Option | Default | Effect |
|---|---|---|
| `--sample` | 1 | Files checked per manuscript directory |
| `--profile` | `sRGB` | Text expected somewhere in the embedded ICC profile's description (case-insensitive substring) |
| `--compression` | `none` | Allowed compression type(s): `none`, `lzw`, `jpeg`, `jpeg-old`, `packbits` |
| `--bit-depth` | `8` | Allowed bits-per-sample value(s) |
| `--standard-ppi` | 400 | Expected resolution |
| `--enhanced-ppi` | 600 | Second allowed resolution tier for flagged items; `0` disallows a second tier |
| `--ppi-tolerance` | 1.0 | ± ppi allowed as a match |
| `--out` | `output/<folder-name>-report.csv` | CSV report path |

Run with `--help` for the full list.

## Output

Prints each directory's result in the terminal as it's checked. The CSV
is written a row at a time as each directory finishes, so an interrupted run
(Ctrl-C, dropped connection) still leaves partial, usable results.

By default the report is written to `output/<folder-name>-report.csv`,
inside an `output/` folder next to the script itself — not the current
directory — so it lands in the same place regardless of where the script is
run from. Rerunning on the same root overwrites its previous report. 
Pass `--out` for a different path.

Findings have three severities:

- `ERROR` — a hard mismatch against the delivery spec (wrong compression,
  bit depth, color mode, missing/wrong profile, off-spec resolution)
- `REVIEW` — needs a human look, not necessarily wrong (e.g. resolution at
  the enhanced tier — confirm the item was actually flagged for it;
  asymmetric x/y resolution; an ICC profile that couldn't be parsed;
  an unexpected channel count)
- `NOTE` — defined for future use, currently unused

## Notes

- **Color profile matching is a substring check, not exact.** Different
  tools embed different literal description strings for the same
  colorimetric sRGB — e.g. `sRGB IEC61966-2.1` (Mac/Adobe) vs
  `IEC 61966-2.1 Default RGB colour space - sRGB` (Windows/HP). Matching on
  the substring `sRGB` catches both. Trade-off: a profile that happens to
  mention "sRGB" without actually being sRGB would pass silently — not a
  realistic risk against a known vendor pipeline, but worth knowing if
  pointed at mixed or unknown sources. For a stricter one-off check, pass a
  longer, more specific `--profile` value.
- **Only reads TIFF header tags, never pixel data**, so it stays fast
  regardless of file size — this is what makes it practical to run against
  a network mount.
- **Other specs**: defaults match the Islamic Manuscripts Initiative
  delivery spec. For a different spec, it is possible to override the relevant flags.
