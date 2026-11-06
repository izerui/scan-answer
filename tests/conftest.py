from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "guanzong199"


@pytest.fixture(scope="session")
def tpl_bases():
    from scan_answer.template import load_template
    return load_template(TEMPLATE_DIR)


@pytest.fixture(scope="session")
def ctx(tpl_bases):
    from scan_answer.engine.recognize import TemplateContext
    t, bases = tpl_bases
    return TemplateContext(t, bases)
