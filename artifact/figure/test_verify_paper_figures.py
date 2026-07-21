import tempfile
import unittest
from pathlib import Path

from PIL import Image
from verify_paper_figures import MAX_RASTER_MAE, raster_mae


class VerifyPaperFiguresTest(unittest.TestCase):
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
