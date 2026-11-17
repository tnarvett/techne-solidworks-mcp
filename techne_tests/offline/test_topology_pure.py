"""Offline tests for the pure parts of ext_topology (no SolidWorks). Run: python -m pytest -q"""

import os
import sys
import tempfile
import types

import pytest

# stand-ins for the Windows-only modules the package imports at load time
for name in ("pythoncom", "win32com", "win32com.client", "win32com.client.gencache",
             "win32com.client.dynamic"):
    mod = sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["win32com"].client = sys.modules["win32com.client"]
sys.modules["win32com.client"].gencache = sys.modules["win32com.client.gencache"]
sys.modules["win32com.client"].dynamic = sys.modules["win32com.client.dynamic"]
sys.modules["win32com.client"].VARIANT = object
sys.modules["pythoncom"].PumpWaitingMessages = lambda: None
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from solidworks_mcp import ext_topology as T  # noqa: E402
from solidworks_mcp.errors import SolidWorksError  # noqa: E402

SLDMAT = """<?xml version="1.0" encoding="UTF-16"?>
<mstns:materials xmlns:mstns="http://www.solidworks.com/sldmaterials" version="2008.03">
  <classification name="Aluminium Alloys">
    <material name="6061 Alloy" matid="1">
      <physicalproperties>
        <EX displayname="Elastic Modulus" value="69000000000"/>
        <SIGYLD displayname="Yield Strength" value="55148500"/>
      </physicalproperties>
    </material>
    <material name="6061-T6 (SS)" matid="2">
      <physicalproperties>
        <SIGYLD displayname="Yield Strength" value="275000000"/>
      </physicalproperties>
    </material>
  </classification>
</mstns:materials>
"""


def test_yield_from_sldmat():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "m.sldmat")
        with open(path, "w", encoding="utf-16") as fh:
            fh.write(SLDMAT)
        assert T.yield_from_sldmat([path], "6061 Alloy") == pytest.approx(55.1485)
        assert T.yield_from_sldmat([os.path.join(d, "missing.sldmat"), path], "6061-T6 (SS)") == 275.0
        assert T.yield_from_sldmat([path], "Unobtainium") is None


def test_stress_to_fos():
    assert T.stress_to_fos(150, 275) == pytest.approx(275 / 150)
    assert T.stress_to_fos(150, 275, 2.5) == 2.5           # explicit FoS stricter
    assert T.stress_to_fos(100, 275, 1.5) == pytest.approx(2.75)  # stress limit stricter
    assert T.stress_to_fos(None, None, 2.0) == 2.0
    with pytest.raises(SolidWorksError, match="above the material's yield"):
        T.stress_to_fos(150, 55.15)
    with pytest.raises(SolidWorksError, match="no yield"):
        T.stress_to_fos(150, None)
    with pytest.raises(SolidWorksError, match="at least 1"):
        T.stress_to_fos(None, 275, 0.5)


def test_mass_target():
    assert T.mass_target(2.0, 40) == pytest.approx(1.2)
    assert T.mass_target(2.0, None, 0.9) == 0.9
    assert T.mass_target(2.0, 40, 0.9) == 0.9
    assert T.mass_target(None, 40) is None


def test_study_name():
    assert T._study_name("B1_0.6") == "B1_0_6"
    assert T._study_name("Topology Techne") == "Topology Techne"
    assert T._study_name("") == "Topology_Techne"
    assert len(T._study_name("x" * 200)) == 60


def test_split_code_check():
    obj = object()
    assert T._split((0, obj)) == (obj, 0)
    assert T._split((17, None)) == (None, 17)
    assert T._split(5) == (None, 5)
    assert T._code((0, 7)) == 7
    assert T._code(0) == 0
    assert T._code(None) == 0
    T._check(0, "x", {})
    with pytest.raises(SolidWorksError, match="between 1 and 10000"):
        T._check(5, "factor of safety constraint", T._ERR["fos"])


def test_view_axes_are_right_handed():
    # screen right x screen up must point at the viewer (out of the screen)
    vec = {"+x": (1, 0, 0), "-x": (-1, 0, 0), "+y": (0, 1, 0), "-y": (0, -1, 0), "+z": (0, 0, 1), "-z": (0, 0, -1)}
    viewer = {"front": (0, 0, 1), "back": (0, 0, -1), "top": (0, 1, 0), "bottom": (0, -1, 0),
              "right": (1, 0, 0), "left": (-1, 0, 0)}
    for view, (r, u) in T.VIEW_AXES.items():
        a, b = vec[r], vec[u]
        cross = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
        assert cross == viewer[view], view
