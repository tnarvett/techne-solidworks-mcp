"""Techne Evaluate tools: measure, section properties, geometry check, mass
properties about a coordinate system. Results in mm, mm^2, mm^4, kg, kg*mm^2, degrees."""

import math

import pythoncom

from . import binding
from .errors import SolidWorksError
from .ext_core import dispatch_array
from .units import m_to_mm

_MEASURE_LEN = ("Distance", "NormalDistance", "CenterDistance", "Diameter", "Radius", "Length", "TotalLength",
                "ArcLength", "ChordLength", "Perimeter", "Projection", "DeltaX", "DeltaY", "DeltaZ", "X", "Y", "Z")
_MEASURE_AREA = ("Area", "TotalArea")


class Evaluate:
    def __init__(self, core):
        self.c = core
        self.s = core.s

    @property
    def mod(self):
        return self.s._mod

    def measure(self, entities: list) -> dict:
        """Measure one or more entities like the Measure tool: distances, angle, diameter,
        lengths, areas, center (x, y, z). -1 values (not applicable) are left out."""
        model = self.s._require_model()
        self.c.select(entities, False, model)
        msr = binding.wrap(self.c.ext(model).CreateMeasure(), self.mod.IMeasure)
        if not msr.Calculate(None):
            model.ClearSelection2(True)
            raise SolidWorksError("SolidWorks could not measure that selection.")
        out = {}
        for prop in _MEASURE_LEN:
            v = getattr(msr, prop)
            if v != -1:
                out[prop.lower() + "_mm"] = round(m_to_mm(v), 5)
        for prop in _MEASURE_AREA:
            v = getattr(msr, prop)
            if v != -1:
                out[prop.lower() + "_mm2"] = round(v * 1e6, 4)
        if msr.Angle != -1:
            out["angle_deg"] = round(math.degrees(msr.Angle), 5)
        for flag in ("IsParallel", "IsPerpendicular", "IsIntersect"):
            out[flag] = bool(getattr(msr, flag))
        model.ClearSelection2(True)
        return {"ok": True, "entities": len(entities), "measure": out}

    def section_properties(self, faces: list) -> dict:
        """Section properties of planar faces (a cut face, a tube end, a port section):
        area, centroid, second moments about the centroid, polar moment, principal values."""
        model = self.s._require_model()
        ents = [self.c.resolve(f, model) for f in faces]
        raw = self.c.ext(model).GetSectionProperties2(dispatch_array(ents))
        vals = list(raw or ())
        if not vals or vals[0] != 0:
            raise SolidWorksError(f"SolidWorks could not compute section properties (status {vals[0] if vals else None}); "
                                  "give planar faces that lie in one plane.")
        mm4 = lambda v: round(v * 1e12, 4)  # noqa: E731
        return {"ok": True, "area_mm2": round(vals[1] * 1e6, 4),
                "centroid_mm": [round(m_to_mm(v), 4) for v in vals[2:5]],
                "Ixx_mm4": mm4(vals[5]), "Iyy_mm4": mm4(vals[6]), "Izz_mm4": mm4(vals[7]),
                "Ixy_mm4": mm4(vals[8]), "Iyz_mm4": mm4(vals[9]), "Ixz_mm4": mm4(vals[10]),
                "polar_J_mm4": mm4(vals[11]), "principal_angle_deg": round(math.degrees(vals[12]), 4),
                "principal_I_mm4": [mm4(vals[13]), mm4(vals[14])],
                "note": "moments about the centroid, in the part's x/y/z"}

    def check_geometry(self) -> dict:
        """Check every body for faults (bad faces, edges, self-intersection), as Tools > Check."""
        self.s._reading += 1
        try:
            model = self.s._current_model()
            part = binding.wrap(model, self.mod.IPartDoc)
            out = []
            for kind, label in ((0, "solid"), (1, "sheet")):
                for raw in part.GetBodies2(kind, False) or ():
                    body = binding.wrap(raw, self.mod.IBody2)
                    raw_faults = body.Check2()
                    if isinstance(raw_faults, int):  # no fault object: the count itself
                        count, faults = raw_faults, None
                    else:
                        faults = binding.wrap(raw_faults, self.mod.IFaultEntity)
                        count = faults.Count if faults is not None else 0
                    entry = {"body": body.Name, "kind": label, "faults": count}
                    if count and faults is not None:
                        entry["codes"] = [faults.ErrorCode(i) for i in range(min(count, 20))]
                    out.append(entry)
            return {"ok": True, "bodies": out, "clean": all(b["faults"] == 0 for b in out)}
        finally:
            self.s._reading -= 1

    def mass_properties_about(self, coordinate_system: str | None = None) -> dict:
        """Mass, center of mass, inertia tensor and principal moments, about the center of
        mass and (if given) a coordinate system by name (add_coordinate_system)."""
        self.s._reading += 1
        try:
            model = self.s._current_model()
            ext = self.c.ext(model)
            mp = binding.wrap(ext.CreateMassProperty2(), self.mod.IMassProperty2)
            mp.UseSystemUnits = True
            kgmm2 = lambda v: round(v * 1e6, 6)  # noqa: E731
            out = {"ok": True, "mass_kg": round(mp.Mass, 6), "volume_mm3": round(mp.Volume * 1e9, 3),
                   "center_of_mass_mm": [round(m_to_mm(v), 4) for v in mp.CenterOfMass],
                   "inertia_at_com_kg_mm2": [kgmm2(v) for v in mp.GetMomentOfInertia(0)],
                   "principal_moments_kg_mm2": [kgmm2(v) for v in mp.PrincipalMomentsOfInertia]}
            if coordinate_system:
                xform = ext.GetCoordinateSystemTransformByName(coordinate_system)
                if xform is None:
                    raise SolidWorksError(f"No coordinate system '{coordinate_system}'.")
                mp.SetCoordinateSystem(xform)
                mp.Recalculate()
                out["coordinate_system"] = coordinate_system
                out["center_of_mass_in_cs_mm"] = [round(m_to_mm(v), 4) for v in mp.CenterOfMass]
                out["inertia_at_cs_kg_mm2"] = [kgmm2(v) for v in mp.GetMomentOfInertia(1)]
            out["note"] = "tensors are row-major Ixx Ixy Ixz / Iyx Iyy Iyz / Izx Izy Izz"
            return out
        finally:
            self.s._reading -= 1
