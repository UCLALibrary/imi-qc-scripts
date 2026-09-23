"""TIFF technical-spec QC. See README.md for what it checks and why."""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import warnings
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageCms

# Tag 33723 (IPTC-NAA) overflow warnings are about embedded caption/keyword
# metadata, unrelated to anything checked here.
warnings.filterwarnings("ignore", category=UserWarning, module=r"PIL\.TiffImagePlugin")

# --- TIFF tag IDs and spec defaults ---

TAG_BITS_PER_SAMPLE = 258
TAG_COMPRESSION = 259
TAG_PHOTOMETRIC = 262
TAG_SAMPLES_PER_PIXEL = 277

PHOTOMETRIC_RGB = 2

COMPRESSION_NAMES = {1: "none", 5: "lzw", 6: "jpeg-old", 7: "jpeg", 32773: "packbits"}
COMPRESSION_CODES = {name: code for code, name in COMPRESSION_NAMES.items()}

DEFAULT_EXPECTED_PROFILE = "sRGB"
PPI_TOLERANCE = 1.0
IMAGE_EXTENSIONS = (".tif", ".tiff")
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


# --- Data types ---


@dataclass
class Finding:
    directory: str
    file: str
    severity: str
    code: str
    message: str


@dataclass
class Spec:
    allowed_compression: set[int]
    allowed_bit_depths: set[int]
    standard_ppi: int
    enhanced_ppi: int | None
    expected_profile: str
    ppi_tolerance: float = PPI_TOLERANCE

    @property
    def allowed_ppi(self) -> set[int]:
        values = {self.standard_ppi}
        if self.enhanced_ppi:
            values.add(self.enhanced_ppi)
        return values


# --- Directory discovery ---


def iter_ms_directories(root: Path):
    """Yields (directory, sorted image filenames) for every directory under
    root that directly contains image files."""
    for dirpath, _dirnames, filenames in os.walk(root):
        tifs = sorted(name for name in filenames if name.lower().endswith(IMAGE_EXTENSIONS))
        if tifs:
            yield Path(dirpath), tifs


# --- Per-file checks ---


def check_file(directory: Path, path: Path, spec: Spec) -> list[Finding]:
    rel_dir = str(directory)
    try:
        with Image.open(path) as img:
            tags = dict(img.tag_v2) if hasattr(img, "tag_v2") else {}
            icc_bytes = img.info.get("icc_profile")
            dpi = img.info.get("dpi")
    except Exception as exc:
        return [Finding(rel_dir, path.name, "ERROR", "unreadable", f"could not open file: {exc}")]

    findings = []
    findings.extend(_check_compression(rel_dir, path.name, tags, spec))
    findings.extend(_check_bit_depth(rel_dir, path.name, tags, spec))
    findings.extend(_check_channels(rel_dir, path.name, tags))
    findings.extend(_check_profile(rel_dir, path.name, icc_bytes, spec))
    findings.extend(_check_resolution(rel_dir, path.name, dpi, spec))
    return findings


def _check_compression(rel_dir: str, name: str, tags: dict, spec: Spec) -> list[Finding]:
    compression = tags.get(TAG_COMPRESSION)
    if compression in spec.allowed_compression:
        return []
    seen = COMPRESSION_NAMES.get(compression, str(compression))
    expected = ", ".join(sorted(COMPRESSION_NAMES.get(c, str(c)) for c in spec.allowed_compression))
    return [Finding(rel_dir, name, "ERROR", "compression",
                     f"compression is {seen}, expected {expected}")]


def _check_bit_depth(rel_dir: str, name: str, tags: dict, spec: Spec) -> list[Finding]:
    bits = tags.get(TAG_BITS_PER_SAMPLE)
    if bits is None:
        return [Finding(rel_dir, name, "ERROR", "bit-depth", "no BitsPerSample tag found")]
    bit_values = set(bits) if isinstance(bits, (tuple, list)) else {bits}
    if bit_values <= spec.allowed_bit_depths:
        return []
    return [Finding(rel_dir, name, "ERROR", "bit-depth",
                     f"bit depth is {sorted(bit_values)}, expected {sorted(spec.allowed_bit_depths)}")]


def _check_channels(rel_dir: str, name: str, tags: dict) -> list[Finding]:
    findings = []
    photometric = tags.get(TAG_PHOTOMETRIC)
    if photometric != PHOTOMETRIC_RGB:
        findings.append(Finding(rel_dir, name, "ERROR", "color-mode",
                                 f"photometric interpretation is {photometric}, expected {PHOTOMETRIC_RGB} (RGB)"))
    samples = tags.get(TAG_SAMPLES_PER_PIXEL)
    if samples is not None and samples != 3:
        findings.append(Finding(rel_dir, name, "REVIEW", "channel-count",
                                 f"{samples} samples per pixel, expected 3 (RGB, no alpha/extra channels)"))
    return findings


def _check_profile(rel_dir: str, name: str, icc_bytes: bytes | None, spec: Spec) -> list[Finding]:
    if icc_bytes is None:
        return [Finding(rel_dir, name, "ERROR", "no-profile", "no embedded ICC color profile")]
    try:
        profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
        profile_name = ImageCms.getProfileName(profile).strip()
    except Exception as exc:
        return [Finding(rel_dir, name, "REVIEW", "profile-unreadable",
                         f"embedded ICC profile could not be parsed: {exc}")]
    if spec.expected_profile.lower() not in profile_name.lower():
        return [Finding(rel_dir, name, "ERROR", "wrong-profile",
                         f"embedded profile is '{profile_name}', expected something containing '{spec.expected_profile}'")]
    return []


def _check_resolution(rel_dir: str, name: str, dpi: tuple | None, spec: Spec) -> list[Finding]:
    if dpi is None:
        return [Finding(rel_dir, name, "ERROR", "no-resolution", "no resolution/PPI information found")]

    x_dpi, y_dpi = float(dpi[0]), float(dpi[1])
    findings = []
    if abs(x_dpi - y_dpi) > spec.ppi_tolerance:
        findings.append(Finding(rel_dir, name, "REVIEW", "asymmetric-ppi",
                                 f"horizontal/vertical resolution differ: {x_dpi:.0f} x {y_dpi:.0f}"))

    nearest = min(spec.allowed_ppi, key=lambda v: abs(v - x_dpi))
    if abs(x_dpi - nearest) > spec.ppi_tolerance:
        findings.append(Finding(rel_dir, name, "ERROR", "ppi",
                                 f"resolution is {x_dpi:.0f} ppi, not one of {sorted(spec.allowed_ppi)}"))
    elif nearest != spec.standard_ppi:
        findings.append(Finding(rel_dir, name, "REVIEW", "enhanced-ppi",
                                 f"resolution is {x_dpi:.0f} ppi — confirm this item was flagged for enhanced resolution"))
    return findings


# --- Scan + report ---

RANK = {"ERROR": 0, "REVIEW": 1, "NOTE": 2}


def run(root: Path, spec: Spec, sample_count: int, csv_writer=None) -> list[Finding]:
    """Walks root, checking one manuscript directory at a time and streaming
    each directory's result to stdout and csv_writer as it's checked."""
    all_findings = []
    dir_count = clean_count = 0
    print(f"Scanning {root} ...", flush=True)
    for directory, tif_names in iter_ms_directories(root):
        dir_count += 1
        findings = []
        for name in tif_names[:sample_count]:
            findings.extend(check_file(directory, directory / name, spec))
        if findings:
            hits = sorted(findings, key=lambda f: (RANK[f.severity], f.code))
            print(f"\n  {directory}:", flush=True)
            for f in hits:
                print(f"    [{f.severity:<6}] {f.code:<16} {f.file:<28} {f.message}")
        else:
            clean_count += 1
            print(f"  {directory}: clean", flush=True)
        if csv_writer is not None:
            for f in findings:
                csv_writer.writerow([f.directory, f.file, f.severity, f.code, f.message])
        all_findings.extend(findings)

    print(f"\n{'=' * 70}")
    print(f"{root}  —  {dir_count} director{'y' if dir_count == 1 else 'ies'} checked, "
          f"{clean_count} clean")
    totals = defaultdict(int)
    for f in all_findings:
        totals[f.severity] += 1
    line = ", ".join(f"{totals[s]} {s.lower()}" for s in RANK if totals[s])
    print(f"Totals: {line or 'nothing flagged'}")
    return all_findings


# --- CLI ---


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check delivered TIFFs' technical specs (color profile, bit depth, "
                     "channels, compression, resolution) against the imaging spec.")
    parser.add_argument("root", type=Path, help="Root directory to walk (e.g. a mounted batch folder)")
    parser.add_argument("--sample", type=int, default=1,
                         help="Files to check per ms directory (default: 1)")
    parser.add_argument("--profile", default=DEFAULT_EXPECTED_PROFILE,
                         help="Text expected somewhere in the embedded ICC profile's description "
                              "(case-insensitive substring match, e.g. 'sRGB')")
    parser.add_argument("--compression", nargs="+", default=["none"],
                         choices=sorted(COMPRESSION_CODES), metavar="TYPE",
                         help=f"Allowed compression type(s): {sorted(COMPRESSION_CODES)}")
    parser.add_argument("--bit-depth", nargs="+", type=int, default=[8],
                         help="Allowed bits-per-sample value(s) (default: 8)")
    parser.add_argument("--standard-ppi", type=int, default=400)
    parser.add_argument("--enhanced-ppi", type=int, default=600,
                         help="Second allowed resolution tier; pass 0 to disallow one")
    parser.add_argument("--ppi-tolerance", type=float, default=PPI_TOLERANCE)
    parser.add_argument("--out", type=Path, default=None,
                         help="CSV report path (default: output/<folder-name>-report.csv "
                              "beside the script)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    spec = Spec(
        allowed_compression={COMPRESSION_CODES[name] for name in args.compression},
        allowed_bit_depths=set(args.bit_depth),
        standard_ppi=args.standard_ppi,
        enhanced_ppi=args.enhanced_ppi or None,
        expected_profile=args.profile,
        ppi_tolerance=args.ppi_tolerance,
    )
    if args.out:
        out_path = args.out
    else:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUT_DIR / f"{args.root.name or 'report'}-report.csv"
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n", quoting=csv.QUOTE_ALL)
        writer.writerow(["directory", "file", "severity", "code", "message"])
        findings = run(args.root, spec, args.sample, csv_writer=writer)
    print(f"Wrote {len(findings)} rows to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
