import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPECPATH).parent
jieba_datas, jieba_binaries, jieba_hiddenimports = collect_all("jieba")
windows_runtime_binaries = []
if sys.platform == "win32":
    conda_library_bin = Path(sys.prefix) / "Library" / "bin"
    for pattern in (
        "ffi-*.dll",
        "libcrypto-*.dll",
        "libexpat.dll",
        "liblzma.dll",
        "libssl-*.dll",
    ):
        windows_runtime_binaries.extend(
            (str(dll), ".") for dll in conda_library_bin.glob(pattern)
        )

datas = [
    (str(project_root / "schema.sql"), "."),
    (str(project_root / "frontend" / "dist"), "frontend/dist"),
    (str(project_root / "resources" / "souls"), "resources/souls"),
    *jieba_datas,
]
hiddenimports = [
    *jieba_hiddenimports,
    *collect_submodules("uvicorn"),
    *collect_submodules("ddgs"),
]

a = Analysis(
    [str(project_root / "desktop" / "engine_entry.py")],
    pathex=[str(project_root)],
    binaries=[*jieba_binaries, *windows_runtime_binaries],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tracelog-engine",
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
    name="tracelog-engine",
)
