"""Tiny real ecCodes GRIB streams exercise backend behavior, not only mocks."""

from pathlib import Path
import tempfile
import unittest

import eccodes as codes
import numpy as np

from src.eda.grib_loader import audit_messages, open_grib


def message(hour=0, level_type="surface", level=0, value_scale=1):
    handle = codes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        settings = {"paramId": 210072, "typeOfLevel": level_type, "level": level,
                    "dataDate": 20030101, "dataTime": hour * 100, "Ni": 2, "Nj": 2,
                    "latitudeOfFirstGridPointInDegrees": 9., "longitudeOfFirstGridPointInDegrees": 102.,
                    "latitudeOfLastGridPointInDegrees": 8.25, "longitudeOfLastGridPointInDegrees": 102.75,
                    "iDirectionIncrementInDegrees": .75, "jDirectionIncrementInDegrees": .75}
        for key, value in settings.items():
            codes.codes_set(handle, key, value)
        codes.codes_set_values(handle, np.array([1e-9, 2e-9, 3e-9, 4e-9]) * value_scale)
        return codes.codes_get_message(handle)
    finally:
        codes.codes_release(handle)


class RealGribTests(unittest.TestCase):
    def test_duplicate_messages_are_audited_even_if_backend_collapses_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pm1.grib"
            source.write_bytes(message() + message() + message(3))
            original = source.read_bytes()
            with open_grib(source, "pm1", root / "cache", chunk_time=1) as (array, key, _):
                audit = audit_messages(source, array.attrs, {}, root / "cache", key)
                self.assertEqual(array.sizes["time"], 2)
                self.assertEqual(audit["message_count"], 3)
                self.assertEqual(audit["duplicate_messages"], 1)
                self.assertEqual(audit["identical_duplicate_messages"], 1)
                self.assertEqual(audit["conflicting_duplicate_messages"], 0)
                self.assertEqual(array.attrs["units"], "kg m**-3")
                np.testing.assert_allclose(array.isel(time=0).compute().data, [[1e-9, 2e-9], [3e-9, 4e-9]])
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(list(root.glob("*.idx")))

    def test_conflicting_duplicate_is_not_treated_as_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pm1.grib"
            source.write_bytes(message() + message(value_scale=2) + message(3))
            with open_grib(source, "pm1", root / "cache") as (array, key, _):
                audit = audit_messages(source, array.attrs, {}, root / "cache", key)
                self.assertEqual(audit["duplicate_messages"], 1)
                self.assertEqual(audit["identical_duplicate_messages"], 0)
                self.assertEqual(audit["conflicting_duplicate_messages"], 1)

    def test_heterogeneous_levels_require_filter_and_filtered_metadata_is_correct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pm1.grib"
            source.write_bytes(message() + message(3, "heightAboveGround", 10))
            with self.assertRaisesRegex(ValueError, "hypercubes|Hypercubes|Ambiguous"):
                with open_grib(source, "pm1", root / "cache"):
                    pass
            with open_grib(source, "pm1", root / "cache", filters={"typeOfLevel": "surface"}) as (array, key, _):
                self.assertEqual(array.attrs["GRIB_typeOfLevel"], "surface")
                self.assertEqual(array.sizes["time"], 1)
                audit = audit_messages(source, array.attrs, {"typeOfLevel": "surface"}, root / "cache", key)
                self.assertEqual(audit["message_count"], 1)


if __name__ == "__main__":
    unittest.main()
