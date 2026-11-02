import pytest

from src.presenters.controllers import next_save_stem


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("rom_v1", "rom_v2"),
        ("rom-v2", "rom-v3"),
        ("rom-v10", "rom-v11"),
        ("romv10", "romv10-edited"),
        ("rom v3", "rom v3-edited"),
        ("rom_V14", "rom_V15"),
        ("rom_v09", "rom_v10"),
        ("rom_v99", "rom_v100"),
        ("AZ1G301F-2008-JDM-Subaru-Impreza-STI", "AZ1G301F-2008-JDM-Subaru-Impreza-STI-edited"),
        ("rom-edited", "rom-edited"),
        ("rom_v1_final", "rom_v1_final-edited"),
    ],
)
def test_next_save_stem(stem: str, expected: str) -> None:
    assert next_save_stem(stem) == expected
