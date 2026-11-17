"""Techne mesh tools: open STL / OBJ / 3MF / PLY files in SolidWorks as a graphics body,
a mesh BREP solid or surface, using SolidWorks' own import options."""

import os

from . import binding
from .errors import SolidWorksError
from .ext_core import enum_value, enums

_MESH_EXT = (".stl", ".obj", ".3mf", ".ply", ".ply2", ".off", ".wrl")


class Mesh:
    def __init__(self, core):
        self.c = core
        self.s = core.s

    def _model_type_value(self, mode):
        """swImportStlVrmlModelType takes an enum whose members name graphics / solid / surface."""
        wanted = {"graphics": "graphic", "solid": "solid", "surface": "surface"}[mode]
        for name, members in enums().items():
            if "stlvrml" in name.lower() or "importstl" in name.lower():
                for member, value in members.items():
                    if wanted in member.lower():
                        return value
        return {"graphics": 0, "solid": 1, "surface": 2}[mode]

    def import_mesh(self, path: str, mode: str = "graphics", units: str = "mm",
                    mesh_brep: bool = True) -> dict:
        """Open a mesh file as a new part. mode: 'graphics' (a reference body to trace or
        segment), 'solid' or 'surface' (mesh BREP bodies to cut, fillet or Boolean)."""
        if mode not in ("graphics", "solid", "surface"):
            raise SolidWorksError("mode is graphics, solid or surface.")
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            raise SolidWorksError(f"No file '{path}'.")
        if not path.lower().endswith(_MESH_EXT):
            raise SolidWorksError(f"Not a mesh file ({', '.join(_MESH_EXT)}); use open_part for STEP/IGES/Parasolid.")
        sw = self.c.sw()
        unit_values = {"mm": "swMM", "cm": "swCM", "m": "swMETER", "in": "swINCHES", "inch": "swINCHES"}
        old = {}
        toggle = old_toggle = None
        try:
            for pref, value in (("swImportStlVrmlModelType", self._model_type_value(mode)),
                                ("swImportStlVrmlUnits", enum_value(unit_values.get(units, "swMM")))):
                key = enum_value(pref)
                old[key] = sw.GetUserPreferenceIntegerValue(key)
                sw.SetUserPreferenceIntegerValue(key, value)
            toggle = enum_value("swVrmlStlImportAsPSMesh")
            old_toggle = sw.GetUserPreferenceToggle(toggle)
            sw.SetUserPreferenceToggle(toggle, bool(mesh_brep and mode != "graphics"))
            ret = sw.LoadFile4(path, "r", None, 0)
            model = ret[0] if isinstance(ret, tuple) else ret
            if model is None:
                raise SolidWorksError(f"SolidWorks could not open '{path}' as a mesh.")
            model = binding.wrap(model, self.s._mod.IModelDoc2)
        finally:
            for key, value in old.items():
                sw.SetUserPreferenceIntegerValue(key, value)
            if toggle is not None:
                sw.SetUserPreferenceToggle(toggle, old_toggle)
        self.s._model = model
        part = binding.wrap(model, self.s._mod.IPartDoc)
        solids = len(part.GetBodies2(0, False) or ())
        sheets = len(part.GetBodies2(1, False) or ())
        return {"ok": True, "document": model.GetTitle(), "mode": mode, "solid_bodies": solids,
                "surface_bodies": sheets,
                "next": "slice_mesh / compare_with_mesh measure it; build the parametric part beside it"}
