"""run_api_script: run Python against the live SolidWorks API on the COM thread.

Added for Tommy's local build (0.20.0.post1). The fixed tools cover part
modelling; anything they do not (drawing views, section/auxiliary views,
dimensions, notes, sheet setup, display options) can be done exactly through
the SolidWorks API instead of by clicking in the window.

The script runs on the server's single COM thread, so it may call any
SolidWorks API method. Names available to the script:

  sw        early-bound ISldWorks
  mod       generated wrapper module (mod.IModelDoc2, mod.IDrawingDoc, mod.IView,
            mod.ISheet, mod.IDisplayDimension, mod.IModelDocExtension, ...)
  wrap      wrap(obj, mod.ISomething) -> early-bound object (None stays None)
  doc       the active document as IModelDoc2 (None if nothing is open)
  drw       the active document as IDrawingDoc when it is a drawing, else None
  ext       doc.Extension as IModelDocExtension (None if no document)
  session   the server's SolidWorksSession
  IN        0.0254 (metres per inch; the API works in metres)
  sel(name, typ, x=0, y=0, z=0, append=False, mark=0)
            doc.Extension.SelectByID2 shortcut; coordinates in metres
  views()   {name: IView} for every view on the current drawing sheet
  VARIANT, pythoncom, win32com, math, os, json, time
  print     collected and returned in 'stdout' (does not touch the MCP pipe)

Set `result` to return a JSON-able value.
"""

import json
import math
import os
import time
import traceback

import pythoncom
import win32com.client

from . import binding

_SW_DOC_DRAWING = 3


def run(session, code: str) -> dict:
    sw = session._ensure()
    mod = session._mod or binding.module()
    raw = sw.ActiveDoc
    doc = binding.wrap(raw, mod.IModelDoc2) if raw is not None else None
    drw = None
    ext = None
    if doc is not None:
        ext = binding.wrap(doc.Extension, mod.IModelDocExtension)
        if int(doc.GetType()) == _SW_DOC_DRAWING:
            drw = binding.wrap(raw, mod.IDrawingDoc)

    out = []

    def _print(*args, sep=" ", end="", **_kw):
        out.append(sep.join(str(a) for a in args) + (end or ""))

    def sel(name, typ, x=0.0, y=0.0, z=0.0, append=False, mark=0):
        if ext is None:
            raise RuntimeError("No active document to select in.")
        return bool(ext.SelectByID2(name, typ, float(x), float(y), float(z), bool(append), int(mark), None, 0))

    def views():
        if drw is None:
            raise RuntimeError("The active document is not a drawing.")
        found = {}
        v = binding.wrap(drw.GetFirstView(), mod.IView)  # the sheet itself
        v = binding.wrap(v.GetNextView(), mod.IView) if v is not None else None
        while v is not None:
            found[v.GetName2()] = v
            v = binding.wrap(v.GetNextView(), mod.IView)
        return found

    ns = {
        "sw": sw, "mod": mod, "wrap": binding.wrap, "doc": doc, "drw": drw, "ext": ext,
        "session": session, "IN": 0.0254, "sel": sel, "views": views,
        "VARIANT": win32com.client.VARIANT, "pythoncom": pythoncom, "win32com": win32com,
        "math": math, "os": os, "json": json, "time": time, "print": _print, "result": None,
    }
    try:
        exec(compile(code, "<run_api_script>", "exec"), ns)  # noqa: S102 - local tool by design
    except Exception:  # noqa: BLE001 - report the script's own error to the agent
        return {"ok": False, "error": traceback.format_exc()[-4000:], "stdout": "\n".join(out)[-8000:]}
    res = ns.get("result")
    try:
        json.dumps(res)
    except (TypeError, ValueError):
        res = repr(res)
    return {"ok": True, "result": res, "stdout": "\n".join(out)[-12000:]}
