import tempfile
import unittest
from pathlib import Path

from PIL import Image
from verify_paper_figures import MAX_RASTER_MAE, raster_mae, verify_json


class VerifyPaperFiguresTest(unittest.TestCase):
    def test_json_verification_ignores_formatting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.json"
            actual = root / "actual.json"
            expected.write_text('{"metric": 1, "values": [2, 3]}\n')
            actual.write_text('{\n  "values": [2, 3],\n  "metric": 1\n}\n')

            verify_json(expected, actual)

    def test_json_verification_rejects_value_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.json"
            actual = root / "actual.json"
            expected.write_text('{"metric": 1}\n')
            actual.write_text('{"metric": 2}\n')

            with self.assertRaisesRegex(ValueError, "structured output mismatch"):
                verify_json(expected, actual)

    def test_raster_mae_allows_small_rendering_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.png"
            actual = root / "actual.png"
            Image.new("RGBA", (20, 20), "white").save(expected)
            changed = Image.new("RGBA", (20, 20), "white")
            changed.putpixel((3, 4), (0, 0, 0, 255))
            changed.save(actual)

            self.assertLess(raster_mae(expected, actual), MAX_RASTER_MAE)

    def test_raster_mae_rejects_dimension_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.png"
            actual = root / "actual.png"
            Image.new("RGBA", (20, 20), "white").save(expected)
            Image.new("RGBA", (21, 20), "white").save(actual)

            with self.assertRaisesRegex(ValueError, "dimensions differ"):
                raster_mae(expected, actual)


if __name__ == "__main__":
    unittest.main()
