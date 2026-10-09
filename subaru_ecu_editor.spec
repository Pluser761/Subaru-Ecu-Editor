# -*- mode: python ; coding: utf-8 -*-
# Сборка PyInstaller: pyinstaller subaru_ecu_editor.spec → dist/SubaruEcuEditor/
# definitions/ecu_defs.xml кладётся рядом с пакетом src: main_presenter.DEFAULT_DEFS
# ищет его относительно своего файла, в сборке это корень _internal.
from PyInstaller.utils.hooks import collect_submodules

# uvicorn и MCP SDK подгружают протоколы и транспорты по имени, анализатор их не видит
hiddenimports = collect_submodules("uvicorn") + collect_submodules("mcp.server")

a = Analysis(
    ["main.py"],
    datas=[("definitions/ecu_defs.xml", "definitions")],
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
)
coll = COLLECT(exe, a.binaries, a.datas, name="SubaruEcuEditor")
