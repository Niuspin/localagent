# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 配置：onedir（启动快、杀软误报少），无控制台窗口。构建：python packaging/build.py
from pathlib import Path

ROOT = Path(SPECPATH).parent

hidden = [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "multipart", "lxml.etree", "lxml._elementpath", "docx", "openpyxl", "pptx", "pypdf", "pdfplumber", "reportlab",
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
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LocalAgent", console=False, icon=None)
coll = COLLECT(exe, a.binaries, a.datas, name="LocalAgent")
