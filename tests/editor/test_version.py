"""Номер следующего релиза (используется кнопкой Release в GitHub Actions)."""

import pytest

from src.version import FIRST_VERSION, next_version, version


@pytest.mark.parametrize(
    ("last", "bump", "expected"),
    [
        ("v0.3.1", "patch", "0.3.2"),
        ("v0.3.1", "minor", "0.4.0"),
        ("v0.3.1", "major", "1.0.0"),
        (None, "patch", FIRST_VERSION),
    ],
)
def test_bump(last, bump, expected):
    assert next_version(last, bump) == expected


def test_explicit_version_must_grow():
    assert next_version("v0.3.1", "patch", "1.0.0") == "1.0.0"
    with pytest.raises(ValueError, match="not greater"):
        next_version("v0.3.1", "patch", "0.3.1")
    with pytest.raises(ValueError, match="X.Y.Z"):
        next_version("v0.3.1", "patch", "1.0")


def test_version_from_source_is_not_empty():
    assert version()
