"""Every shipped mapping must parse and validate."""
from __future__ import annotations

import pytest

from plumb import config, mapping


def test_mappings_directory_is_found():
    config.resolve_dirs()
    assert config.MAPPINGS_DIR.is_dir(), config.MAPPINGS_DIR


@pytest.mark.parametrize("path", mapping.available_mappings(), ids=lambda p: p.stem)
def test_mapping_loads(path):
    spec = mapping.load(str(path))
    spec.validate()
    assert spec.account.name
    assert spec.column("posted_on")
