from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import download_cams_dataset as pipeline
from src.download_cams_dataset import (
    TIMES,
    VIETNAM_AREA,
    build_request,
    is_grib_file,
    merge_grib_files,
    merged_file_is_current,
    merged_file_path,
    yearly_file_path,
)


def fake_grib(payload: bytes) -> bytes:
    return b"GRIB" + payload + b"7777"


class RequestTests(unittest.TestCase):
    def test_request_contains_one_variable_and_one_year(self) -> None:
        request = build_request("particulate_matter_2.5um", 2004)

        self.assertEqual(request["variable"], ["particulate_matter_2.5um"])
        self.assertEqual(request["date"], ["2004-01-01/2004-12-31"])
        self.assertEqual(request["time"], list(TIMES))
        self.assertEqual(request["area"], list(VIETNAM_AREA))
        self.assertEqual(request["data_format"], "grib")
        self.assertNotIn("download_format", request)

    def test_default_range_has_23_yearly_requests_per_variable(self) -> None:
        self.assertEqual(len(range(2003, 2025 + 1)), 23)

    def test_default_selects_all_twenty_variables(self):
        args = pipeline.build_parser().parse_args([])
        self.assertEqual(args.variables, list(pipeline.VARIABLES))
        self.assertEqual(len(args.variables), 20)
        self.assertEqual((args.start_year, args.end_year), (2003, 2025))

    def test_only_multilevel_requests_select_model_level_60(self):
        for name, variable in pipeline.VARIABLES.items():
            with self.subTest(name=name):
                request = build_request(variable, 2025)
                self.assertEqual(request["variable"], [variable])
                self.assertEqual(request["area"], [24., 102., 6., 118.5])
                self.assertEqual(request["time"], list(TIMES))
                if name in pipeline.CAMS_MULTI_LEVEL_VARIABLES:
                    self.assertEqual(request["model_level"], ["60"])
                else:
                    self.assertNotIn("model_level", request)

    def test_unknown_variable_is_rejected_before_request(self):
        with self.assertRaises(SystemExit):
            pipeline.build_parser().parse_args(["--variables", "not_a_variable"])


class PathTests(unittest.TestCase):
    def test_paths_follow_yearly_and_merged_layout(self) -> None:
        root = Path("data/CAMS/EAC4")

        self.assertEqual(
            yearly_file_path(root, "pm2p5", 2003),
            root / "yearly" / "pm2p5" / "pm2p5_2003_vietnam.grib",
        )
        self.assertEqual(
            merged_file_path(root, "pm2p5", 2003, 2025),
            root / "merged" / "pm2p5_2003_2025_vietnam.grib",
        )


class MergeTests(unittest.TestCase):
    def test_merge_concatenates_yearly_grib_streams_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "pm2p5_2003_vietnam.grib"
            second = root / "pm2p5_2004_vietnam.grib"
            output = root / "merged" / "pm2p5_2003_2004_vietnam.grib"
            first.write_bytes(fake_grib(b"-2003-"))
            second.write_bytes(fake_grib(b"-2004-"))

            changed = merge_grib_files([first, second], output)

            self.assertTrue(changed)
            self.assertTrue(is_grib_file(output))
            self.assertEqual(output.read_bytes(), first.read_bytes() + second.read_bytes())
            self.assertTrue(merged_file_is_current(output, [first, second]))

    def test_invalid_source_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "invalid.grib"
            output = root / "merged.grib"
            invalid.write_bytes(b"not-grib")

            with self.assertRaises(FileNotFoundError):
                merge_grib_files([invalid], output)

            self.assertFalse(output.exists())


class FakeCdsClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], Path]] = []

    def retrieve(
        self, dataset: str, request: dict[str, object], target: str
    ) -> None:
        target_path = Path(target)
        self.calls.append((dataset, request, target_path))
        target_path.write_bytes(fake_grib(str(request["date"]).encode("ascii")))


class PipelineTests(unittest.TestCase):
    def test_multilevel_and_single_level_selection_download_and_merge_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeCdsClient()
            selected = ["pm2p5", *pipeline.CAMS_MULTI_LEVEL_VARIABLES]
            args = pipeline.build_parser().parse_args([
                "--data-root", directory, "--variables", *selected, "co",
                "--start-year", "2003", "--end-year", "2004", "--request-delay", "0",
            ])
            with patch.object(pipeline, "create_cds_client", return_value=client), patch.object(
                pipeline, "validate_yearly_file"
            ):
                self.assertEqual(pipeline.run_pipeline(args), 0)
            self.assertEqual(len(client.calls), len(selected) * 2)
            for i, name in enumerate(selected):
                calls = client.calls[2 * i:2 * i + 2]
                for year, (_, request, _) in zip((2003, 2004), calls):
                    self.assertEqual(request, build_request(pipeline.VARIABLES[name], year))
                annuals = [yearly_file_path(Path(directory), name, y) for y in (2003, 2004)]
                merged = merged_file_path(Path(directory), name, 2003, 2004)
                self.assertEqual(merged.read_bytes(), b"".join(p.read_bytes() for p in annuals))

    def test_pipeline_sends_one_request_per_year_and_creates_merged_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "CAMS" / "EAC4"
            client = FakeCdsClient()
            args = pipeline.build_parser().parse_args(
                [
                    "--start-year",
                    "2003",
                    "--end-year",
                    "2004",
                    "--variables",
                    "pm2p5",
                    "--data-root",
                    str(data_root),
                    "--max-attempts",
                    "1",
                    "--request-delay",
                    "0",
                ]
            )

            with patch.object(pipeline, "create_cds_client", return_value=client), patch.object(
                pipeline, "validate_yearly_file"
            ):
                result = pipeline.run_pipeline(args)

            self.assertEqual(result, 0)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(
                [call[1]["date"] for call in client.calls],
                [["2003-01-01/2003-12-31"], ["2004-01-01/2004-12-31"]],
            )
            output = merged_file_path(data_root, "pm2p5", 2003, 2004)
            self.assertTrue(is_grib_file(output))

    def test_processes_all_years_of_one_variable_before_next(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeCdsClient()
            args = pipeline.build_parser().parse_args([
                "--data-root", directory, "--variables", "pm1", "pm10",
                "--start-year", "2003", "--end-year", "2004", "--request-delay", "0",
            ])
            with patch.object(pipeline, "create_cds_client", return_value=client), patch.object(
                pipeline, "validate_yearly_file"
            ):
                self.assertEqual(pipeline.run_pipeline(args), 0)
            self.assertEqual([c[1]["variable"][0] for c in client.calls], [
                pipeline.VARIABLES["pm1"], pipeline.VARIABLES["pm1"],
                pipeline.VARIABLES["pm10"], pipeline.VARIABLES["pm10"],
            ])

    def test_resume_does_not_initialize_api_or_modify_existing_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            yearly = yearly_file_path(root, "pm1", 2003)
            yearly.parent.mkdir(parents=True)
            yearly.write_bytes(fake_grib(b"-2003-"))
            merged = merged_file_path(root, "pm1", 2003, 2003)
            merge_grib_files([yearly], merged)
            before = (merged.read_bytes(), merged.stat().st_mtime_ns)
            args = pipeline.build_parser().parse_args([
                "--data-root", directory, "--variables", "pm1",
                "--start-year", "2003", "--end-year", "2003",
            ])
            with patch.object(pipeline, "create_cds_client", side_effect=AssertionError("No API")), patch.object(
                pipeline, "validate_yearly_file"
            ):
                self.assertEqual(pipeline.run_pipeline(args), 0)
            self.assertEqual((merged.read_bytes(), merged.stat().st_mtime_ns), before)

    def test_invalid_download_does_not_replace_year_or_merge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            annual = yearly_file_path(root, "pm1", 2003)
            annual.parent.mkdir(parents=True)
            annual.write_bytes(fake_grib(b"original"))
            merged = merged_file_path(root, "pm1", 2003, 2003)
            merge_grib_files([annual], merged)
            before = (annual.read_bytes(), merged.read_bytes())
            args = pipeline.build_parser().parse_args([
                "--data-root", directory, "--variables", "pm1", "--overwrite",
                "--start-year", "2003", "--end-year", "2003", "--max-attempts", "1",
            ])
            with patch.object(pipeline, "create_cds_client", return_value=FakeCdsClient()), patch.object(
                pipeline, "validate_yearly_file", side_effect=ValueError("Incomplete year")
            ):
                self.assertEqual(pipeline.run_pipeline(args), 1)
            self.assertEqual((annual.read_bytes(), merged.read_bytes()), before)
            self.assertFalse(pipeline.partial_path(annual).exists())


if __name__ == "__main__":
    unittest.main()
