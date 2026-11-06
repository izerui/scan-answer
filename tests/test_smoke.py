def test_imports():
    import cv2
    import numpy
    import zxingcpp
    from PIL import Image

    import scan_answer

    assert int(cv2.__version__.split(".")[0]) >= 5
    assert scan_answer.__name__ == "scan_answer"
