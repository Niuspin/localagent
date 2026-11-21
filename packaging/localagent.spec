# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 配置：onedir（启动快、杀软误报少），无控制台窗口。构建：python packaging/build.py
from pathlib import Path

ROOT = Path(SPECPATH).parent

hidden = [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "multipart", "lxml.etree", "lxml._elementpath", "yaml", "sqlite3",
    "docx", "openpyxl", "pptx", "pypdf", "pdfplumber", "reportlab",
    "webview", "clr_loader", "pythonnet", "lagent.bootstrap",
    "lagent.swarm", "lagent.mission", "lagent.media", "lagent.research", "lagent.skills", "lagent.metrics",
]
datas = [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "lagent" / "builtin_skills"), "lagent/builtin_skills"),
]
a = Analysis(
    [str(ROOT / "run.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=hidden,
    excludes=["tkinter", "pytest", "playwright", "IPython", "matplotlib", "numpy.tests"],
)
pyz = PYZ(a.pure)
_ico = ROOT / "web" / "icons" / "app-icon.ico"
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LocalAgent", console=False, icon=str(_ico) if _ico.is_file() else None)
coll = COLLECT(exe, a.binaries, a.datas, name="LocalAgent")
