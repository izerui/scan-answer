import pytest

from scan_answer.app.settings import (AppSettings, bundled_templates_dir, config_dir, load_settings, save_settings,
                                      settings_path)
from scan_answer.ocr.config import OcrConfig


def test_defaults_roundtrip_and_ocr(app_home):
    assert config_dir() == app_home
    s = load_settings()
    assert s.workers == 2 and s.scan.dpi == 300 and s.ocr is None and s.ocr_config() is None
    assert str(bundled_templates_dir()) in s.template_dirs
    s.workers = 3
    s.scan.dpi = 600
    s.set_ocr_config(OcrConfig("https://x/v1", "k", "m", concurrency=2))
    save_settings(s)
    again = load_settings()
    assert again.workers == 3 and again.scan.dpi == 600
    assert again.ocr_config() == OcrConfig("https://x/v1", "k", "m", concurrency=2)


def test_corrupt_settings_fall_back_to_defaults(app_home):
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text("{oops", encoding="utf-8")
    assert load_settings() == AppSettings()


def test_invalid_ocr_raises(app_home):
    s = AppSettings(ocr={"base_url": "ftp://x", "api_key": "", "model": "m"})
    with pytest.raises(ValueError):
        s.ocr_config()


def test_wrong_types_fall_back_to_defaults(app_home):
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    import json
    settings_path().write_text(json.dumps({
        "workers": "three", "template_dirs": 42, "scan": {"dpi": 123, "color": "x"}, "operator": 5
    }), encoding="utf-8")
    s = load_settings()
    defaults = AppSettings()
    assert s.workers == defaults.workers
    assert s.template_dirs == defaults.template_dirs
    assert s.scan.dpi == defaults.scan.dpi
    assert s.scan.color == defaults.scan.color
    assert s.operator == defaults.operator


def test_non_object_settings_fall_back_to_defaults(app_home):
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text("[]", encoding="utf-8")
    assert load_settings() == AppSettings()


def test_ocr_fields_validated(app_home):
    import json
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    # 类型不对的可选字段丢弃，其余保留
    settings_path().write_text(json.dumps({"ocr": {"base_url": "https://x/v1", "api_key": "k", "model": "m",
                                                   "concurrency": "4", "timeout_s": "slow", "max_retries": 1}}),
                               encoding="utf-8")
    s = load_settings()
    assert s.ocr == {"base_url": "https://x/v1", "api_key": "k", "model": "m", "max_retries": 1}
    assert s.ocr_config() == OcrConfig("https://x/v1", "k", "m", max_retries=1)
    # 必填字段缺失或类型不对：视为未配置转写
    for bad in ({"base_url": "https://x/v1", "api_key": "k"}, {"base_url": 1, "api_key": "k", "model": "m"}, [1]):
        settings_path().write_text(json.dumps({"ocr": bad}), encoding="utf-8")
        assert load_settings().ocr is None
