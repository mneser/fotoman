# -*- mode: python ; coding: utf-8 -*-
"""Windows onedir build. Run from the repo root on Windows:

    python -m pip install pyinstaller
    python -m PyInstaller --noconfirm packaging/fotoman.spec

Output: dist/Fotoman/Fotoman.exe
Must be built on Windows — this spec does not cross-compile.
"""

from pathlib import Path

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "fotoman" / "static"), "fotoman/static"),
    (str(ROOT / "countries.json"), "."),
]
binaries = []
hiddenimports = [
    "fotoman.api",
    "fotoman.browse",
    "fotoman.config",
    "fotoman.db",
    "fotoman.duplicates",
    "fotoman.embed",
    "fotoman.events",
    "fotoman.faces",
    "fotoman.geocode",
    "fotoman.index_store",
    "fotoman.indexer",
    "fotoman.metadata",
    "fotoman.properties",
    "fotoman.search",
    "fotoman.vfolders",
    "fotoman.vqa",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "multipart",
    "tkinter",
    "tkinter.filedialog",
]

for pkg in (
    "transformers",
    "torch",
    "cv2",
    "faiss",
    "PIL",
    "reverse_geocoder",
    "sentencepiece",
):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

hiddenimports += collect_submodules("fotoman")

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "IPython"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Fotoman",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Fotoman",
)
