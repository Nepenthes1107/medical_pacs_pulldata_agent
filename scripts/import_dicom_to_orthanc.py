import argparse
import os
from pathlib import Path

import requests


DICOM_EXTENSIONS = {".dcm"}


def iter_files(root: Path):
    for current_root, _, files in os.walk(root, followlinks=True):
        for name in files:
            if name.startswith("."):
                continue
            path = Path(current_root) / name
            if path.is_file() and path.suffix.lower() in DICOM_EXTENSIONS:
                yield path


def iter_series_dirs(root: Path):
    """Yield series subdirectories sorted by name."""
    if not root.is_dir():
        return
    dirs = sorted(
        d for d in root.iterdir()
        if d.is_dir() or d.is_symlink()
    )
    yield from dirs


def upload_file(path: Path, orthanc_url: str) -> bool:
    with path.open("rb") as fp:
        response = requests.post(
            "%s/instances" % orthanc_url.rstrip("/"),
            data=fp,
            timeout=30,
        )
    if response.status_code in (200, 201):
        return True
    print("[FAIL] %s status=%s body=%s" % (path, response.status_code, response.text[:200]))
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Import DICOM files into Orthanc recursively.")
    parser.add_argument("--input-dir", default="data/dicom_samples")
    parser.add_argument("--orthanc-url", default="http://localhost:8042")
    parser.add_argument("--limit", type=int, default=0, help="Only upload the first N files when greater than 0.")
    parser.add_argument("--series-limit", type=int, default=0, help="Only upload the first N series (alphabetically) when greater than 0.")
    args = parser.parse_args()

    root = Path(args.input_dir)
    if not root.exists():
        raise SystemExit("input directory not found: %s" % root)

    # Determine which directories to scan
    subdirs = list(iter_series_dirs(root))
    if not subdirs:
        # No series subdirs — scan root directly
        subdirs = [root]

    if args.series_limit and args.series_limit > 0:
        selected = subdirs[: args.series_limit]
        skipped_dirs = subdirs[args.series_limit :]
        if skipped_dirs:
            print("Using first %s of %s series; skipped: %s" % (
                args.series_limit,
                len(subdirs),
                ", ".join(d.name for d in skipped_dirs),
            ))
    else:
        selected = subdirs
        print("Using all %s series" % len(subdirs))

    ok = fail = skipped = total = 0
    for series_dir in selected:
        for path in iter_files(series_dir):
            if args.limit and total >= args.limit:
                break
            total += 1
            try:
                if upload_file(path, args.orthanc_url):
                    ok += 1
                else:
                    fail += 1
            except Exception as exc:
                fail += 1
                print("[ERROR] %s: %s" % (path, exc))
        if args.limit and total >= args.limit:
            break

    print("total=%s, ok=%s, fail=%s, skipped=%s" % (total, ok, fail, skipped))


if __name__ == "__main__":
    main()
