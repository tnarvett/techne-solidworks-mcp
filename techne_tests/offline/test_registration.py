"""Register the Techne tools on a real FastMCP server with stand-ins for Windows modules."""
import asyncio, os, sys, types

for name in ("pythoncom", "pywintypes", "win32com", "win32com.client", "win32com.client.gencache",
             "win32com.client.dynamic"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["win32com"].client = sys.modules["win32com.client"]
for attr in ("gencache", "dynamic"):
    setattr(sys.modules["win32com.client"], attr, sys.modules[f"win32com.client.{attr}"])
sys.modules["win32com.client"].VARIANT = object
sys.modules["win32com.client"].Dispatch = lambda *a, **k: None
pc = sys.modules["pythoncom"]
for attr in ("VT_ARRAY", "VT_R8", "VT_DISPATCH", "VT_VARIANT", "VT_I4", "VT_BSTR", "VT_BOOL", "VT_BYREF"):
    setattr(pc, attr, 0)
pc.com_error = Exception
pc.PumpWaitingMessages = lambda: None
sys.modules["pywintypes"].com_error = Exception
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from solidworks_mcp import ext_server  # noqa: E402


class FakeSession:
    _quiet_depth = 0


def test_register():
    mcp = FastMCP("t")
    tools = ext_server.register(mcp, lambda fn, *a, **k: None, FakeSession(), None, lambda e: {"ok": False})
    listed = asyncio.run(mcp.list_tools())
    names = {t.name for t in listed}
    for must in ("topology_study", "topology_sweep", "topology_verify", "topology_views", "sim_delete", "split_line"):
        assert must in names
    print(len(listed), "tools")
    ts = next(t for t in listed if t.name == "topology_study")
    props = ts.inputSchema["properties"]
    assert {"displacement_component", "preserve_load_faces", "replace"} <= set(props)
    assert len(listed) == len(tools)
