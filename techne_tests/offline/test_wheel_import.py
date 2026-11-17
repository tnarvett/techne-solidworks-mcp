"""Import the built wheel's full server on Linux (Windows modules stubbed) and count its tools."""
import asyncio, glob, os, sys, types, zipfile, tempfile, shutil, difflib, html, queue, functools, uuid  # noqa: E401
import importlib.resources, xml.sax.saxutils, subprocess, argparse, json, threading, concurrent.futures  # noqa: E401,F401
import mcp.server.fastmcp  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))


def _stub():
    for name in ("pythoncom", "pywintypes", "win32com", "win32com.client", "win32com.client.gencache",
                 "win32com.client.dynamic", "win32gui", "win32con", "win32api", "win32process"):
        sys.modules.setdefault(name, types.ModuleType(name))
    w = sys.modules["win32com.client"]
    sys.modules["win32com"].client = w
    w.gencache = sys.modules["win32com.client.gencache"]
    w.dynamic = sys.modules["win32com.client.dynamic"]
    for attr in ("VARIANT", "Dispatch", "GetActiveObject", "GetObject", "CastTo"):
        setattr(w, attr, lambda *a, **k: None)
    pc = sys.modules["pythoncom"]
    for a in ("VT_ARRAY", "VT_R8", "VT_DISPATCH", "VT_VARIANT", "VT_I4", "VT_BSTR", "VT_BOOL", "VT_BYREF", "VT_I2",
              "VT_UNKNOWN", "VT_EMPTY", "COINIT_APARTMENTTHREADED"):
        setattr(pc, a, 0)
    pc.com_error = Exception
    for f in ("PumpWaitingMessages", "CoInitializeEx", "CoInitialize", "CoUninitialize"):
        setattr(pc, f, lambda *a: None)
    sys.modules["pywintypes"].com_error = Exception


def test_wheel_lists_all_tools():
    found = sorted(glob.glob(os.path.join(HERE, "..", "..", "solidworks_mcp-*-py3-none-any.whl")))
    if not found:
        import pytest
        pytest.skip("no wheel built yet: run python build_wheel.py")
    whl = found[-1]
    dest = tempfile.mkdtemp()
    zipfile.ZipFile(whl).extractall(dest)
    _stub()
    for k in [k for k in sys.modules if k == "solidworks_mcp" or k.startswith("solidworks_mcp.")]:
        del sys.modules[k]
    sys.path.insert(0, dest)
    real = sys.platform
    sys.platform = "win32"
    try:
        import solidworks_mcp
        import solidworks_mcp.server as server
    finally:
        sys.platform = real
        sys.path.remove(dest)
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    print(solidworks_mcp.__version__, len(tools), "tools")
    src_init = open(os.path.join(HERE, "..", "..", "src", "solidworks_mcp", "__init__.py"), encoding="utf-8").read()
    assert solidworks_mcp.__version__ in src_init
    assert {"run_api_script", "topology_study", "topology_verify", "split_line", "sim_static"} <= names
    assert len(tools) == 141
