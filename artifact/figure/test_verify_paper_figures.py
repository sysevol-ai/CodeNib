import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw
from verify_paper_figures import (
    MAX_DIMENSION_DRIFT,
    MAX_PHASH_DISTANCE,
    MAX_THUMBNAIL_MAE,
    raster_metrics,
    verify_json,
)


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
            baseline = Image.new("RGBA", (64, 64), "white")
            draw = ImageDraw.Draw(baseline)
            draw.rectangle((8, 8, 56, 56), outline="black", width=2)
            draw.line((8, 48, 56, 20), fill="blue", width=3)
            baseline.save(expected)
            changed = baseline.copy()
            changed.putpixel((3, 4), (0, 0, 0, 255))
            changed.save(actual)

            dimension, phash, thumbnail = raster_metrics(expected, actual)

            self.assertLess(dimension, MAX_DIMENSION_DRIFT)
            self.assertLess(phash, MAX_PHASH_DISTANCE)
            self.assertLess(thumbnail, MAX_THUMBNAIL_MAE)

    def test_raster_mae_rejects_dimension_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.png"
            actual = root / "actual.png"
            Image.new("RGBA", (20, 20), "white").save(expected)
            Image.new("RGBA", (21, 20), "white").save(actual)

            dimension, _, _ = raster_metrics(expected, actual)

            self.assertGreater(dimension, MAX_DIMENSION_DRIFT)

    def test_raster_metrics_reject_major_pixel_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "expected.png"
            actual = root / "actual.png"
            Image.new("RGBA", (20, 20), "white").save(expected)
            Image.new("RGBA", (20, 20), "black").save(actual)

            _, _, thumbnail = raster_metrics(expected, actual)

            self.assertGreater(thumbnail, MAX_THUMBNAIL_MAE)


if __name__ == "__main__":
    unittest.main()
