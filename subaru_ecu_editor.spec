# -*- mode: python ; coding: utf-8 -*-
# Сборка PyInstaller: pyinstaller subaru_ecu_editor.spec → dist/SubaruEcuEditor/
# (на macOS ещё dist/SubaruEcuEditor.app). Иконки — assets/ (исходник icon.svg).
# definitions/ecu_defs.xml кладётся рядом с пакетом src: main_presenter.DEFAULT_DEFS
# ищет его относительно своего файла, в сборке это корень _internal.
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# uvicorn и MCP SDK подгружают протоколы и транспорты по имени, анализатор их не видит
hiddenimports = collect_submodules("uvicorn") + collect_submodules("mcp.server")

a = Analysis(
    ["main.py"],
    datas=[("definitions/ecu_defs.xml", "definitions"), ("assets/icon.png", "assets")],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "mypy"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SubaruEcuEditor",
    console=False,
    icon="assets/icon.ico",  # на macOS не используется: иконка .app — в BUNDLE
)
coll = COLLECT(exe, a.binaries, a.datas, name="SubaruEcuEditor")

if sys.platform == "darwin":
    # Номер, который workflow вписал в src/version.py; macOS принимает только X.Y.Z
    match = re.search(r'__version__ = "(\d+\.\d+\.\d+)', Path("src/version.py").read_text(encoding="utf-8"))
    bundle_version = match.group(1) if match else "0.0.0"
    app = BUNDLE(
        coll,
        name="SubaruEcuEditor.app",
        icon="assets/icon.icns",
        bundle_identifier="io.github.subaru-ecu-editor",
        version=bundle_version,
        info_plist={"CFBundleDisplayName": "Subaru ECU Editor", "NSHighResolutionCapable": True},
    )
