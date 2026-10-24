"""Download and merge CAMS EAC4 data for Vietnam.

Each variable/year pair is submitted as one independent ADS request. The yearly
GRIB files are kept so interrupted runs can resume, then concatenated in
chronological order into one multi-year GRIB file per variable.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


DATASET = "cams-global-reanalysis-eac4"
DEFAULT_START_YEAR = 2003
DEFAULT_END_YEAR = 2025

# EAC4 provides one value every three hours.
TIMES = (
    "00:00",
    "03:00",
    "06:00",
    "09:00",
    "12:00",
    "15:00",
    "18:00",
    "21:00",
)

# CAMS/CDS order: [North, West, South, East].
VIETNAM_AREA = (24.0, 102.0, 6.0, 118.5)

CAMS_SINGLE_LEVEL_VARIABLES = {
    # Particulate matter
    "pm1": "particulate_matter_1um",
    "pm2p5": "particulate_matter_2.5um",
    "pm10": "particulate_matter_10um",
    # Wind at 10 m
    "u10": "10m_u_component_of_wind",
    "v10": "10m_v_component_of_wind",
    # Temperature and dew point at 2 m
    "t2m": "2m_temperature",
    "d2m": "2m_dewpoint_temperature",
    # Pressure
    "sp": "surface_pressure",
    "mslp": "mean_sea_level_pressure",
    # Aerosol optical depth
    "aod550": "total_aerosol_optical_depth_550nm",
    # Other pollutants
    "co_column": "total_column_carbon_monoxide",
    "no2_column": "total_column_nitrogen_dioxide",
    "so2_column": "total_column_sulphur_dioxide",
    "o3_column": "total_column_ozone",
}

CAMS_MULTI_LEVEL_VARIABLES = {
    "co": "carbon_monoxide",
    "no2": "nitrogen_dioxide",
    "so2": "sulphur_dioxide",
    "o3": "ozone",
    "t_model": "temperature",
    "q_model": "specific_humidity",
}
# EAC4's lowest model level; distinct from 2 m and total-column fields.
CAMS_MODEL_LEVEL = ("60",)
CAMS_TIMES = TIMES
VARIABLES = {**CAMS_SINGLE_LEVEL_VARIABLES, **CAMS_MULTI_LEVEL_VARIABLES}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "CAMS" / "EAC4"
YEARLY_DIR_NAME = "yearly"
MERGED_DIR_NAME = "merged"
COPY_BUFFER_SIZE = 16 * 1024 * 1024
LOGGER = logging.getLogger("cams_eac4")

PARAM_IDS = {
    "pm1": 210072, "pm2p5": 210073, "pm10": 210074,
    "u10": 165, "v10": 166, "t2m": 167, "d2m": 168,
    "sp": 134, "mslp": 151, "aod550": 210207,
    "co_column": 210127, "no2_column": 210125,
    "so2_column": 210126, "o3_column": 210206,
    "co": 210123, "no2": 210121, "so2": 210122, "o3": 210203,
    "t_model": 130, "q_model": 133,
}


class GribMessageCountError(ValueError):
    """A yearly stream has a missing or repeated message count."""


def validate_yearly_file(path: Path, short_name: str, year: int) -> None:
    """Reject incomplete years, duplicate messages, wrong parameters and old grids.

    Disable cfgrib sidecars so validation never changes the input directory.
    Only one year's decoded values are held in memory at a time.
    """
    import calendar
    import eccodes as ec
    import numpy as np
    import pandas as pd
    import xarray as xr

    if not is_grib_file(path):
        raise ValueError(f"Not a complete GRIB stream: {path}")
    expected = pd.date_range(
        f"{year}-01-01", f"{year + 1}-01-01", freq="3h", inclusive="left"
    )
    # ecCodes/CFFI keeps a C FILE* until the Python stream is released on
    # Windows. Release it before cfgrib opens the same file (and before rename).
    stream = path.open("rb")
    try:
        message_count = ec.codes_count_in_file(stream)
    finally:
        stream.close()
        del stream
    if message_count != (366 if calendar.isleap(year) else 365) * 8:
        raise GribMessageCountError(
            f"Missing or duplicate GRIB messages: got {message_count}, "
            f"expected {len(expected)} for {year}: {path}"
        )
    north, west, south, east = VIETNAM_AREA
    with xr.open_dataset(
        path, engine="cfgrib", backend_kwargs={"indexpath": "", "errors": "raise"}
    ) as ds:
        if len(ds.data_vars) != 1:
            raise ValueError(f"Expected exactly one variable: {path}")
        field = next(iter(ds.data_vars.values()))
        if field.attrs.get("GRIB_paramId") != PARAM_IDS[short_name]:
            raise ValueError(f"Unexpected CAMS parameter for {short_name}: {path}")
        if short_name in CAMS_MULTI_LEVEL_VARIABLES:
            if (
                field.attrs.get("GRIB_typeOfLevel") != "hybrid"
                or "hybrid" not in ds.coords
                or ds.hybrid.ndim != 0
                or ds.hybrid.item() != int(CAMS_MODEL_LEVEL[0])
            ):
                raise ValueError(f"Expected model level 60 for {short_name}: {path}")
        if set(field.dims) != {"time", "latitude", "longitude"}:
            raise ValueError(f"Unexpected dimensions: {field.dims}")
        if not np.array_equal(ds.time.values, expected.values):
            raise ValueError(f"Expected the complete 3-hourly UTC year {year}: {path}")
        if "step" not in ds or bool((ds.step != np.timedelta64(0, "ns")).any()):
            raise ValueError(f"Expected analysis step zero: {path}")
        if "valid_time" not in ds or not np.array_equal(ds.valid_time.values, ds.time.values):
            raise ValueError(f"Expected valid_time equal to time: {path}")
        for axis, low, high in (("latitude", south, north), ("longitude", west, east)):
            wanted = np.arange(low, high + 0.375, 0.75)
            actual = np.sort(ds[axis].values)
            if actual.shape != wanted.shape or not np.allclose(actual, wanted, rtol=0, atol=1e-7):
                raise ValueError(f"Expected full mainland and island grid ({axis}): {path}")
        if not np.isfinite(field.values).all():
            raise ValueError(f"Missing or non-finite grid values: {path}")


def yearly_file_is_valid(path: Path, short_name: str, year: int) -> bool:
    if not path.is_file():
        return False
    try:
        validate_yearly_file(path, short_name, year)
        return True
    except Exception as error:
        LOGGER.warning("[VALIDATE][FAILED] %s: %s", path, error)
        return False


def configure_logging(level: str, log_file: Path | None = None) -> None:
    """Configure timestamped progress logs for the terminal."""
    # Windows PowerShell may expose stdout as cp1252 even when the source file
    # and messages are UTF-8. Reconfigure it before emitting Vietnamese logs.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        # Some redirected/test streams do not expose reconfigure().
        pass

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    for old_handler in LOGGER.handlers:
        old_handler.close()
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    if log_file is not None:
        log_file = resolve_data_root(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(handler.formatter)
        LOGGER.addHandler(file_handler)
    LOGGER.setLevel(level)
    LOGGER.propagate = False


def format_bytes(byte_count: int) -> str:
    value = float(byte_count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining_seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m {remaining_seconds}s"


def build_request(variable_name: str, year: int) -> dict[str, object]:
    """Build one ADS request for exactly one variable and one calendar year."""
    request: dict[str, object] = {
        "variable": [variable_name],
        "date": [f"{year}-01-01/{year}-12-31"],
        "time": list(TIMES),
        "data_format": "grib",
        "area": list(VIETNAM_AREA),
    }
    if variable_name in CAMS_MULTI_LEVEL_VARIABLES.values():
        request["model_level"] = list(CAMS_MODEL_LEVEL)
    return request


def yearly_file_path(data_root: Path, short_name: str, year: int) -> Path:
    return (
        data_root
        / YEARLY_DIR_NAME
        / short_name
        / f"{short_name}_{year}_vietnam.grib"
    )


def merged_file_path(
    data_root: Path,
    short_name: str,
    start_year: int,
    end_year: int,
) -> Path:
    return (
        data_root
        / MERGED_DIR_NAME
        / f"{short_name}_{start_year}_{end_year}_vietnam.grib"
    )


def is_grib_file(path: Path) -> bool:
    """Perform a lightweight check for a raw, non-empty GRIB stream."""
    if not path.is_file() or path.stat().st_size < 12:
        return False

    with path.open("rb") as source:
        if source.read(4) != b"GRIB":
            return False
        source.seek(-4, 2)
        return source.read(4) == b"7777"


def partial_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.part")


def remove_identical_duplicate_messages(path: Path) -> int:
    """Remove only byte-identical GRIB messages from a temporary download.

    ADS can return an annual stream with repeated messages (e.g. t_model 2019).
    Conflicting values or metadata are retained for strict validation to reject.
    The published annual files are never edited here.
    """
    import eccodes as ec

    normalized = path.with_name(f"{path.name}.dedup")
    seen: set[bytes] = set()
    duplicate_count = 0
    try:
        stream = path.open("rb")
        try:
            with normalized.open("wb") as destination:
                while (handle := ec.codes_grib_new_from_file(stream)) is not None:
                    try:
                        message = ec.codes_get_message(handle)
                    finally:
                        ec.codes_release(handle)
                    if message in seen:
                        duplicate_count += 1
                    else:
                        seen.add(message)
                        destination.write(message)
        finally:
            stream.close()
            del stream
        if duplicate_count:
            LOGGER.warning(
                "[NORMALIZE] %s: bỏ %s message trùng hoàn toàn từng byte; "
                "sẽ kiểm tra lại đầy đủ năm trước khi công bố file.",
                path, duplicate_count,
            )
            normalized.replace(path)
        return duplicate_count
    finally:
        normalized.unlink(missing_ok=True)


def download_year(
    client: Any,
    short_name: str,
    variable_name: str,
    year: int,
    target: Path,
    *,
    max_attempts: int,
    retry_backoff_seconds: float,
    overwrite: bool,
    progress_label: str = "REQUEST",
) -> tuple[bool, bool, int]:
    """Download one yearly file.

    Returns ``(succeeded, request_was_sent, attempts_used)``. Downloads use a
    temporary file so an interrupted request never leaves a file that a later
    run mistakes for a completed year.
    """
    if not overwrite and yearly_file_is_valid(target, short_name, year):
        LOGGER.info(
            "[%s][SKIP] %s %s đã tồn tại: %s",
            progress_label,
            short_name,
            year,
            target,
        )
        return True, False, 0

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = partial_path(target)
    temporary.unlink(missing_ok=True)
    request = build_request(variable_name, year)

    for attempt in range(1, max_attempts + 1):
        attempt_started_at = time.monotonic()
        LOGGER.info(
            "[%s][DOWNLOAD] biến=%s | năm=%s | lần=%s/%s",
            progress_label,
            short_name,
            year,
            attempt,
            max_attempts,
        )
        LOGGER.debug("[%s] Request: %s", progress_label, request)
        LOGGER.debug("[%s] File đích: %s", progress_label, target)
        try:
            client.retrieve(DATASET, request, str(temporary))
            if not is_grib_file(temporary):
                raise RuntimeError(
                    "ADS response is not a raw GRIB stream. "
                    "Check data_format and the server response."
                )
            try:
                validate_yearly_file(temporary, short_name, year)
            except GribMessageCountError:
                if not remove_identical_duplicate_messages(temporary):
                    raise
                validate_yearly_file(temporary, short_name, year)
            temporary.replace(target)
            LOGGER.info(
                "[%s][DONE] biến=%s | năm=%s | dung lượng=%s | thời gian=%s",
                progress_label,
                short_name,
                year,
                format_bytes(target.stat().st_size),
                format_duration(time.monotonic() - attempt_started_at),
            )
            return True, True, attempt
        except Exception as error:  # cdsapi exposes several backend exceptions
            temporary.unlink(missing_ok=True)
            elapsed = format_duration(time.monotonic() - attempt_started_at)
            if attempt == max_attempts:
                LOGGER.error(
                    "[%s][FAILED] biến=%s | năm=%s | sau=%s | lỗi=%s",
                    progress_label,
                    short_name,
                    year,
                    elapsed,
                    error,
                    exc_info=LOGGER.isEnabledFor(logging.DEBUG),
                )
                return False, True, attempt

            wait_seconds = retry_backoff_seconds * (2 ** (attempt - 1))
            LOGGER.warning(
                "[%s][RETRY] biến=%s | năm=%s | sau=%s | lỗi=%s | "
                "thử lại sau=%ss",
                progress_label,
                short_name,
                year,
                elapsed,
                error,
                f"{wait_seconds:g}",
            )
            if wait_seconds:
                time.sleep(wait_seconds)

    return False, True, max_attempts  # Defensive; the loop always returns.


def merged_file_is_current(output: Path, sources: Sequence[Path]) -> bool:
    if not is_grib_file(output) or not sources:
        return False

    expected_size = sum(source.stat().st_size for source in sources)
    newest_source = max(source.stat().st_mtime_ns for source in sources)
    return (
        output.stat().st_size == expected_size
        and output.stat().st_mtime_ns >= newest_source
    )


def merge_grib_files(
    sources: Sequence[Path],
    output: Path,
    *,
    overwrite: bool = False,
) -> bool:
    """Concatenate yearly GRIB streams in the supplied chronological order."""
    if not sources:
        raise ValueError("At least one yearly GRIB file is required.")

    invalid_sources = [path for path in sources if not is_grib_file(path)]
    if invalid_sources:
        missing = "\n  - ".join(str(path) for path in invalid_sources)
        raise FileNotFoundError(f"Missing or invalid yearly GRIB files:\n  - {missing}")

    if not overwrite and merged_file_is_current(output, sources):
        LOGGER.info("[MERGE][SKIP] File đã cập nhật: %s", output)
        return False

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = partial_path(output)
    temporary.unlink(missing_ok=True)
    merge_started_at = time.monotonic()
    total_size = sum(source.stat().st_size for source in sources)
    LOGGER.info(
        "[MERGE][START] %s file năm | tổng=%s | output=%s",
        len(sources),
        format_bytes(total_size),
        output,
    )

    try:
        with temporary.open("wb") as destination:
            for source_index, source_path in enumerate(sources, start=1):
                LOGGER.info(
                    "[MERGE %s/%s] %s | %s",
                    source_index,
                    len(sources),
                    source_path.name,
                    format_bytes(source_path.stat().st_size),
                )
                with source_path.open("rb") as source:
                    shutil.copyfileobj(source, destination, COPY_BUFFER_SIZE)

        if not is_grib_file(temporary) or temporary.stat().st_size != total_size:
            raise RuntimeError("Merged GRIB failed integrity checks.")

        temporary.replace(output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    LOGGER.info(
        "[MERGE][DONE] %s file năm | dung lượng=%s | thời gian=%s | output=%s",
        len(sources),
        format_bytes(output.stat().st_size),
        format_duration(time.monotonic() - merge_started_at),
        output,
    )
    return True


def create_cds_client() -> Any:
    try:
        import cdsapi
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Missing dependency 'cdsapi'. Run: "
            "python -m pip install -r requirements.txt"
        ) from error

    # Keep cdsapi's own queue-status and transfer progress visible as well.
    return cdsapi.Client(quiet=False, progress=True)


def resolve_data_root(path: Path) -> Path:
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def run_pipeline(args: argparse.Namespace) -> int:
    pipeline_started_at = time.monotonic()
    data_root = resolve_data_root(args.data_root)
    selected_variables = unique(args.variables)
    years = list(range(args.start_year, args.end_year + 1))
    should_download = args.stage in {"all", "download"}
    should_merge = args.stage in {"all", "merge"}

    request_count = len(selected_variables) * len(years) if should_download else 0
    LOGGER.info("Bắt đầu CAMS EAC4 pipeline")
    LOGGER.info("Dataset      : %s", DATASET)
    LOGGER.info("Data root    : %s", data_root)
    LOGGER.info("Stage        : %s", args.stage)
    LOGGER.info("Số biến      : %s", len(selected_variables))
    LOGGER.info(
        "Giai đoạn    : %s-%s (%s năm)",
        args.start_year,
        args.end_year,
        len(years),
    )
    LOGGER.info("Số request   : %s request năm (tối đa)", request_count)

    if args.dry_run:
        for variable_index, short_name in enumerate(selected_variables, start=1):
            output = merged_file_path(
                data_root, short_name, args.start_year, args.end_year
            )
            LOGGER.info(
                "[PLAN %s/%s] biến=%s | downloads=%s | merged=%s",
                variable_index,
                len(selected_variables),
                short_name,
                len(years) if should_download else 0,
                output if should_merge else "tắt",
            )
            if should_download:
                LOGGER.info(
                    "[PLAN] Request đầu tiên: %s",
                    build_request(VARIABLES[short_name], years[0]),
                )
        LOGGER.info("Dry-run hoàn tất; chưa gửi request và chưa tạo file.")
        return 0

    client: Any | None = None
    failures: list[tuple[str, int]] = []
    merge_failures: list[str] = []
    sent_requests = 0
    request_attempts = 0
    downloaded_files = 0
    skipped_files = 0
    merged_files = 0
    skipped_merges = 0

    for variable_index, short_name in enumerate(selected_variables, start=1):
        variable_started_at = time.monotonic()
        variable_name = VARIABLES[short_name]
        variable_download_failed = False
        yearly_files = [
            yearly_file_path(data_root, short_name, year) for year in years
        ]
        LOGGER.info(
            "[VARIABLE %s/%s][START] %s (%s)",
            variable_index,
            len(selected_variables),
            short_name,
            variable_name,
        )

        if should_download:
            existing_years: set[int] = set()
            if not args.overwrite:
                for year, target in zip(years, yearly_files):
                    if target.is_file():
                        LOGGER.info("[CHECK] kiểm tra file có sẵn: %s", target)
                        if yearly_file_is_valid(target, short_name, year):
                            existing_years.add(year)
            pending_years = [year for year in years if year not in existing_years]
            LOGGER.info(
                "[RESUME] %s | bỏ qua=%s năm hợp lệ | cần tải=%s | các năm cần tải=%s",
                short_name, len(existing_years), len(pending_years),
                ", ".join(map(str, pending_years)) or "không có",
            )
            for year_index, (year, target) in enumerate(
                zip(years, yearly_files), start=1
            ):
                request_index = (variable_index - 1) * len(years) + year_index
                progress_label = f"REQUEST {request_index}/{request_count}"
                if year in existing_years:
                    skipped_files += 1
                    LOGGER.info(
                        "[%s][SKIP] biến=%s | năm=%s | dung lượng=%s",
                        progress_label,
                        short_name,
                        year,
                        format_bytes(target.stat().st_size),
                    )
                    continue

                if client is None:
                    try:
                        LOGGER.info("Khởi tạo CDS/ADS API client...")
                        client = create_cds_client()
                        LOGGER.info("CDS/ADS API client đã sẵn sàng.")
                    except Exception as error:
                        LOGGER.error(
                            "Không thể khởi tạo API client: %s",
                            error,
                            exc_info=LOGGER.isEnabledFor(logging.DEBUG),
                        )
                        return 1

                if sent_requests and args.request_delay:
                    LOGGER.debug(
                        "Chờ %ss trước request tiếp theo.",
                        f"{args.request_delay:g}",
                    )
                    time.sleep(args.request_delay)

                succeeded, request_was_sent, attempts_used = download_year(
                    client,
                    short_name,
                    variable_name,
                    year,
                    target,
                    max_attempts=args.max_attempts,
                    retry_backoff_seconds=args.retry_backoff,
                    overwrite=args.overwrite,
                    progress_label=progress_label,
                )
                sent_requests += int(request_was_sent)
                request_attempts += attempts_used
                if not succeeded:
                    failures.append((short_name, year))
                    variable_download_failed = True
                else:
                    downloaded_files += int(request_was_sent)

        if should_merge:
            if variable_download_failed:
                LOGGER.error(
                    "[MERGE][SKIP] biến=%s vì có ít nhất một năm tải thất bại.",
                    short_name,
                )
                merge_failures.append(short_name)
                continue

            invalid_years = [
                year
                for year, path in zip(years, yearly_files)
                if not (is_grib_file(path) if should_download else yearly_file_is_valid(path, short_name, year))
            ]
            if invalid_years:
                year_list = ", ".join(str(year) for year in invalid_years)
                LOGGER.error(
                    "[MERGE][SKIP] biến=%s | các năm thiếu/không hợp lệ: %s",
                    short_name,
                    year_list,
                )
                merge_failures.append(short_name)
                continue

            output = merged_file_path(
                data_root, short_name, args.start_year, args.end_year
            )
            try:
                was_merged = merge_grib_files(
                    yearly_files, output, overwrite=args.overwrite
                )
                merged_files += int(was_merged)
                skipped_merges += int(not was_merged)
            except Exception as error:
                LOGGER.error(
                    "[MERGE][FAILED] biến=%s | lỗi=%s",
                    short_name,
                    error,
                    exc_info=LOGGER.isEnabledFor(logging.DEBUG),
                )
                merge_failures.append(short_name)

        variable_duration = format_duration(time.monotonic() - variable_started_at)
        if variable_download_failed or short_name in merge_failures:
            LOGGER.error(
                "[VARIABLE %s/%s][FAILED] %s | thời gian=%s",
                variable_index,
                len(selected_variables),
                short_name,
                variable_duration,
            )
        else:
            LOGGER.info(
                "[VARIABLE %s/%s][DONE] %s | thời gian=%s",
                variable_index,
                len(selected_variables),
                short_name,
                variable_duration,
            )

    if failures:
        failed_jobs = ", ".join(f"{name}/{year}" for name, year in failures)
        LOGGER.error("Các request thất bại: %s", failed_jobs)

    status = "THÀNH CÔNG" if not failures and not merge_failures else "CÓ LỖI"
    LOGGER.info(
        "[SUMMARY] trạng thái=%s | thời gian=%s",
        status,
        format_duration(time.monotonic() - pipeline_started_at),
    )
    LOGGER.info(
        "[SUMMARY] request logic=%s | lượt gọi gồm retry=%s | tải mới=%s | bỏ qua=%s",
        sent_requests,
        request_attempts,
        downloaded_files,
        skipped_files,
    )
    LOGGER.info(
        "[SUMMARY] file ghép mới=%s | file ghép đã cập nhật=%s | "
        "download lỗi=%s | biến chưa ghép=%s",
        merged_files,
        skipped_merges,
        len(failures),
        len(merge_failures),
    )

    return 1 if failures or merge_failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download CAMS EAC4 as one request per variable/year, then merge "
            "the yearly GRIB files into one GRIB per variable."
        )
    )
    parser.add_argument(
        "--start-year", type=int, default=DEFAULT_START_YEAR, help="First year."
    )
    parser.add_argument(
        "--end-year", type=int, default=DEFAULT_END_YEAR, help="Last year."
    )
    parser.add_argument(
        "--variables",
        nargs="+",
        choices=tuple(VARIABLES),
        default=list(VARIABLES),
        metavar="NAME",
        help=(
            "Variable short names to process. Default: all 20 variables. "
            "Single level: " + ", ".join(CAMS_SINGLE_LEVEL_VARIABLES)
            + ". Model level 60: " + ", ".join(CAMS_MULTI_LEVEL_VARIABLES) + "."
        ),
    )
    parser.add_argument(
        "--stage",
        choices=("all", "download", "merge"),
        default="all",
        help="Run both stages, download only, or merge only.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help="CAMS EAC4 data root. Relative paths are resolved from project root.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum download attempts for each yearly request.",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=30.0,
        help="Initial retry wait in seconds; doubles after each failed attempt.",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=2.0,
        help="Delay in seconds between completed yearly requests.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Redownload valid yearly files and rebuild merged files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the plan without creating directories or contacting ADS.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Append logs to a UTF-8 file (relative to project root).",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="Terminal log verbosity. Default: INFO.",
    )
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.start_year < DEFAULT_START_YEAR:
        parser.error(f"EAC4 starts in {DEFAULT_START_YEAR}.")
    if args.end_year < args.start_year:
        parser.error("--end-year must be greater than or equal to --start-year.")
    if args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1.")
    if args.retry_backoff < 0:
        parser.error("--retry-backoff cannot be negative.")
    if args.request_delay < 0:
        parser.error("--request-delay cannot be negative.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)
    configure_logging(args.log_level, None if args.dry_run else args.log_file)
    try:
        return run_pipeline(args)
    except KeyboardInterrupt:
        LOGGER.warning("Đã dừng pipeline theo yêu cầu người dùng (Ctrl+C).")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
