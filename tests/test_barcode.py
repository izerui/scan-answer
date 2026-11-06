import numpy as np
import zxingcpp

from scan_answer.engine.barcode import read_barcode


def test_read_code128_and_none():
    bc = zxingcpp.create_barcode("100015000123456", zxingcpp.BarcodeFormat.Code128)
    img = np.asarray(zxingcpp.write_barcode_to_image(bc, scale=3, add_quiet_zones=True))
    assert read_barcode(img) == ("100015000123456", "Code128")
    assert read_barcode(np.full((80, 200), 255, np.uint8)) is None
