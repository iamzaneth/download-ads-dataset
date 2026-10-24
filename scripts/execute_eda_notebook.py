"""Execute the notebook with this interpreter, save outputs, and validate format.

Usage: python scripts/execute_eda_notebook.py [--pm1-only]
The PM1 check creates a temporary hard link to the real PM1 input, never modifies it.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from jupyter_client import KernelManager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.download_cams_dataset import DEFAULT_DATA_ROOT, merged_file_path, DEFAULT_START_YEAR, DEFAULT_END_YEAR, MERGED_DIR_NAME


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pm1-only", action="store_true")
    args = parser.parse_args()
    runtime = DEFAULT_DATA_ROOT / "eda_cache" / "jupyter_runtime"
    for variable, child in (("IPYTHONDIR", "ipython"), ("JUPYTER_CONFIG_DIR", "config"), ("JUPYTER_DATA_DIR", "data"), ("JUPYTER_RUNTIME_DIR", "runtime"), ("MPLCONFIGDIR", "matplotlib")):
        path = runtime / child
        path.mkdir(parents=True, exist_ok=True)
        os.environ[variable] = str(path)
    source = ROOT / "notebooks" / "01_cams_eac4_eda.ipynb"
    notebook = nbformat.read(source, as_version=4)
    context = tempfile.TemporaryDirectory(prefix="cams-pm1-", dir=DEFAULT_DATA_ROOT / "eda_cache") if args.pm1_only else nullcontext(None)
    with context as folder:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        if args.pm1_only:
            data_root = Path(folder)
            merged = data_root / MERGED_DIR_NAME
            merged.mkdir(parents=True)
            pm1 = merged_file_path(DEFAULT_DATA_ROOT, "pm1", DEFAULT_START_YEAR, DEFAULT_END_YEAR)
            os.link(pm1, merged / pm1.name)
            report_root = ROOT / "reports" / "validation_pm1"
            env.update(CAMS_EDA_DATA_ROOT=str(data_root), CAMS_EDA_REPORT_ROOT=str(report_root), CAMS_EDA_CACHE_ROOT=str(DEFAULT_DATA_ROOT / "eda_cache"))
            destination = report_root / "01_cams_eac4_eda_pm1.ipynb"
        else:
            destination = source
        km = KernelManager(kernel_name="python3")
        # Do not register a global kernel or silently use a different Python environment.
        km.kernel_spec.argv = [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"]
        client = NotebookClient(notebook, km=km, timeout=3600, resources={"metadata": {"path": str(ROOT)}},
                                on_cell_start=lambda cell, cell_index: print(f"Cell {cell_index + 1}/{len(notebook.cells)}", flush=True))
        print(f"Executing {source.name}: {'PM1-only real input' if args.pm1_only else 'all available inputs'}", flush=True)
        try:
            client.execute(env=env)
            nbformat.validate(notebook)
            destination.parent.mkdir(parents=True, exist_ok=True)
            nbformat.write(notebook, destination)
            print(f"Executed notebook: {destination}", flush=True)
        finally:
            # An externally supplied kernel manager is not cleaned up automatically by nbclient.
            if km.has_kernel:
                km.shutdown_kernel(now=True)
            km.cleanup_resources()


if __name__ == "__main__":
    main()
