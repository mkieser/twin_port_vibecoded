import unittest

import numpy as np

from load_rasters import CD_TO_CGVD2013, Raster, adjust_nonna


def make_nonna():
    """Small NONNA-10-like raster: depths in Chart Datum, NoData = -9999."""
    arr = np.array([[-12.3, -5.0, -9999.0],
                    [-0.5, -9999.0, -30.25],
                    [-1.0, -2.0, -3.0]], dtype="float32")
    data = np.ma.masked_equal(arr, -9999.0)
    return Raster("NONNA-10", data, (450000.0, 10.0, 0.0, 4950000.0, 0.0, -10.0),
                  "EPSG:2961", -9999.0)


class TestNonnaDatumShift(unittest.TestCase):
    def test_adjusted_minus_original_is_minus_1_41_m(self):
        original = make_nonna()
        adjusted = adjust_nonna(make_nonna(), -32767.0)

        self.assertEqual(CD_TO_CGVD2013, -1.41)  # chart datum is below CGVD2013 zero
        np.testing.assert_array_equal(adjusted.data.mask, original.data.mask)

        diff = adjusted.data.compressed() - original.data.compressed()
        np.testing.assert_allclose(diff, -1.41, atol=1e-5)

    def test_nodata_cells_are_not_shifted(self):
        adjusted = adjust_nonna(make_nonna(), -32767.0)
        nodata_cells = adjusted.data.data[adjusted.data.mask]
        np.testing.assert_array_equal(nodata_cells, -32767.0)
        self.assertEqual(adjusted.nodata, -32767.0)


if __name__ == "__main__":
    unittest.main()
