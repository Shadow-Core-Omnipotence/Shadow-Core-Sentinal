# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

# Sentinel is STANDALONE (2026-07-31). It used to add a sibling project's
# directory to pathex and pull in its `telemetry` module for tracing
# middleware. That import had been failing silently for some time --
# TraceMiddleware was deleted from Engineer during its Bare SDK migration --
# so the coupling bought nothing while making this spec raise
# FileNotFoundError on any machine without Shadow-Core Engineer checked out
# next to it. The build now depends on nothing outside this directory.
_HERE = os.path.dirname(os.path.abspath(SPEC))

datas = []
binaries = []
hiddenimports = [
    'watchdog',
    # TASK-S03 follow-up — explicit anyio backend + uvicorn loop/protocol modules.
    # fastmcp.run() uses anyio.run() which needs the asyncio backend. PyInstaller
    # misses it through static analysis. Symptom when missing: silent process
    # that reaches mcp.run() and then hangs without ever binding the port.
    'anyio',
    'anyio._backends',
    'anyio._backends._asyncio',
    'anyio.from_thread',
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.loops.asyncio',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
]

# Bundle 'mcp' package + all metadata
tmp_ret = collect_all('mcp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Bundle 'fastmcp' package + dist-info so importlib.metadata can resolve its version
tmp_ret = collect_all('fastmcp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Bundle 'uvicorn' package + 'anyio' so all backend loops/protocols are present
tmp_ret = collect_all('uvicorn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('anyio')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[_HERE],
    binaries=binaries,
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
    a.binaries,
    a.datas,
    [],
    name='shadow-core-sentinel',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
