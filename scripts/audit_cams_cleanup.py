"""Read-only data audit for the EAC4 directory migration (no ADS requests).

Compare the protected merged files and retained annual files with the saved
cleanup snapshot. This verification never writes to the data tree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.download_cams_dataset import (  # noqa: E402
    DEFAULT_DATA_ROOT,
)

REPORT = ROOT / "reports" / "cams_cleanup_audit.json"


def fingerprint(path):
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    stat = path.stat()
    return {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest}


def audit_after():
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    assert report["before_passed"]
    protected = Path(report["protected_directory"])
    after = {p.name: fingerprint(p) for p in protected.iterdir()}
    assert after == report["protected_before"], "Protected data or timestamps changed!"
    for row in report["annual_files"]:
        actual = fingerprint(Path(row["target"]))
        assert actual == {key: row[key] for key in actual}, row["target"]
    assert len(list((DEFAULT_DATA_ROOT / "yearly").rglob("*.grib"))) == 322
    assert {p.name for p in DEFAULT_DATA_ROOT.iterdir()} == {"yearly", "merged"}
    report.update(after_passed=True, protected_after=after, retained_annual_files=322)
    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("PASS: 322 annual files retained; all 14 protected files have identical SHA-256, sizes and mtimes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after", action="store_true")
    args = parser.parse_args()
    audit_after()
