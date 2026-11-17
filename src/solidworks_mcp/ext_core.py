"""Techne core: generic access to the whole SolidWorks API plus history control.

Added in 0.21.0 for Tommy's build. These tools sit under the hand-made ones:
anything SolidWorks can do is reachable here in a structured way, without
writing a script.

Units: tools named *_mm / *_deg take millimetres and degrees, like the rest of
the server. The generic tools (get_feature, modify_feature, create_feature,
call_api) speak the raw API, which is metres and radians; there any value may
be written with a unit as a string ("12mm", "0.5in", "5deg") and is converted.
An enum member name ("swEndCondBlind") becomes its number.
"""

import contextlib
import datetime
import inspect
import json
import math
import os
import re
import time

import pythoncom
import win32com.client

from . import binding
from .errors import SolidWorksError
from .units import m_to_mm, mm_to_m

VARIANT = win32com.client.VARIANT

# --- storage -----------------------------------------------------------------

def data_dir(*parts) -> str:
    base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Techne", *parts)
    os.makedirs(base, exist_ok=True)
    return base


def log_call(tool: str, result, ms: float) -> None:
    """One line per tool call in %LOCALAPPDATA%\\Techne\\telemetry.jsonl."""
    try:
        ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        entry = {"t": datetime.datetime.now().isoformat(timespec="seconds"), "tool": tool, "ok": ok,
                 "ms": round(ms, 1)}
        if not ok and isinstance(result, dict):
            entry["error"] = str(result.get("error", ""))[:300]
        with open(os.path.join(data_dir(), "telemetry.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001 - telemetry must never break a tool
        pass


# --- values ------------------------------------------------------------------

_UNITS = {"mm": 1e-3, "cm": 1e-2, "m": 1.0, "um": 1e-6, "in": 0.0254, "inch": 0.0254, "ft": 0.3048,
          "deg": math.pi / 180.0, "rad": 1.0, "g": 1e-3, "kg": 1.0, "n": 1.0, "kn": 1e3,
          "pa": 1.0, "kpa": 1e3, "mpa": 1e6, "gpa": 1e9, "psi": 6894.757293168}
_NUM_UNIT = re.compile(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z]+)\s*")

_ENUMS = None      # {enum_name: {member: value}}
_MEMBERS = None    # {member: (enum_name, value)}
_SESSION = None    # the server's session, so enum lookups can connect on their own


def enums(sw=None) -> dict:
    """Every enum of swconst.tlb (about 970), read once from the installed SolidWorks."""
    global _ENUMS, _MEMBERS
    if _ENUMS is None:
        if sw is None and _SESSION is not None:
            sw = _SESSION._ensure()
        if sw is None:
            raise SolidWorksError("Not connected to SolidWorks yet.")
        path = os.path.join(sw.GetExecutablePath(), "swconst.tlb")
        tlb = pythoncom.LoadTypeLib(path)
        out, members = {}, {}
        for i in range(tlb.GetTypeInfoCount()):
            ti = tlb.GetTypeInfo(i)
            ta = ti.GetTypeAttr()
            if ta.typekind != pythoncom.TKIND_ENUM:
                continue
            name = tlb.GetDocumentation(i)[0]
            vals = {}
            for j in range(ta.cVars):
                vd = ti.GetVarDesc(j)
                member = ti.GetNames(vd.memid)[0]
                vals[member] = vd.value
                members.setdefault(member, (name, vd.value))
            out[name] = vals
        _ENUMS, _MEMBERS = out, members
    return _ENUMS


def enum_value(member: str, sw=None) -> int:
    enums(sw)
    if member not in _MEMBERS:
        raise SolidWorksError(f"No SolidWorks enum member '{member}'. Search with api_lookup.")
    return _MEMBERS[member][1]


def enum_name(enum: str, value, sw=None):
    for k, v in enums(sw).get(enum, {}).items():
        if v == value:
            return k
    return value


def to_api(v):
    """'12mm' -> 0.012, '5deg' -> 0.0873, 'swEndCondBlind' -> 0; lists element-wise."""
    if isinstance(v, str):
        m = _NUM_UNIT.fullmatch(v)
        if m and m.group(2).lower() in _UNITS:
            return float(m.group(1)) * _UNITS[m.group(2).lower()]
        if v.startswith("sw") and _MEMBERS and v in _MEMBERS:
            return _MEMBERS[v][1]
        return v
    if isinstance(v, (list, tuple)):
        return [to_api(x) for x in v]
    return v


def jsonable(v, depth: int = 0):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return round(v, 10)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, (list, tuple)):
        if depth > 2:
            return f"<array of {len(v)}>"
        return [jsonable(x, depth + 1) for x in v[:500]]
    if hasattr(v, "_oleobj_") or type(v).__name__ in ("PyIDispatch", "CDispatch"):
        for attr in ("Name", "Name2"):
            try:
                return {"com_object": True, "name": str(getattr(v, attr))}
            except Exception:  # noqa: BLE001
                continue
        return {"com_object": True}
    try:
        json.dumps(v)
        return v
    except TypeError:
        return str(v)


def dispatch_array(objs):
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [getattr(o, "_oleobj_", o) for o in objs])


def double_array(vals):
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(x) for x in vals])


# Feature type (IFeature.GetTypeName2) -> its definition interface.
FEATURE_DATA = {
    "Extrusion": "IExtrudeFeatureData2", "Boss": "IExtrudeFeatureData2", "BossThin": "IExtrudeFeatureData2",
    "Cut": "IExtrudeFeatureData2", "CutThin": "IExtrudeFeatureData2", "ICE": "IExtrudeFeatureData2",
    "Revolution": "IRevolveFeatureData2", "RevolutionThin": "IRevolveFeatureData2", "RevCut": "IRevolveFeatureData2",
    "RevCutThin": "IRevolveFeatureData2", "BaseRevolve": "IRevolveFeatureData2",
    "Sweep": "ISweepFeatureData", "SweepCut": "ISweepFeatureData", "SweepThread": "ISweepFeatureData",
    "Blend": "ILoftFeatureData", "BlendCut": "ILoftFeatureData", "NetBlend": "IBoundaryBossFeatureData",
    "Fillet": "ISimpleFilletFeatureData2", "VarFillet": "IVariableFilletFeatureData2",
    "Chamfer": "IChamferFeatureData2", "Shell": "IShellFeatureData", "Draft": "IDraftFeatureData2",
    "Rib": "IRibFeatureData2", "Dome": "IDomeFeatureData2", "HoleWzd": "IWizardHoleFeatureData2",
    "AdvHoleWzd": "IAdvancedHoleFeatureData", "HoleSeries": "IHoleSeriesFeatureData2",
    "Thread": "IThreadFeatureData", "CosmeticThread": "ICosmeticThreadFeatureData",
    "LPattern": "ILinearPatternFeatureData", "CirPattern": "ICircularPatternFeatureData",
    "MirrorPattern": "IMirrorPatternFeatureData", "MirrorSolid": "IMirrorSolidFeatureData",
    "CurvePattern": "ICurveDrivenPatternFeatureData", "SketchPattern": "ISketchPatternFeatureData",
    "TablePattern": "ITablePatternFeatureData", "FillPattern": "IFillPatternFeatureData",
    "DimPattern": "IDimPatternFeatureData", "ChainPattern": "IChainPatternFeatureData",
    "RefPlane": "IRefPlaneFeatureData", "RefAxis": "IRefAxisFeatureData", "RefPoint": "IRefPointFeatureData",
    "CoordSys": "ICoordinateSystemFeatureData", "Helix": "IHelixFeatureData",
    "CompositeCurve": "ICompositeCurveFeatureData", "SplitLine": "ISplitLineFeatureData",
    "Thicken": "IThickenFeatureData", "ThickenCut": "IThickenFeatureData", "Scale": "IScaleFeatureData",
    "MoveCopyBody": "IMoveCopyBodyFeatureData", "BodyDelete": "IDeleteBodyFeatureData",
    "Combine": "ICombineBodiesFeatureData", "Split": "ISplitBodyFeatureData", "Intersect": "IIntersectFeatureData",
    "Wrap": "IWrapSketchFeatureData", "Indent": "IIndentFeatureData", "DeleteFace": "IDeleteFaceFeatureData",
    "MoveFace": "IMoveFaceFeatureData", "ReplaceFace": "IReplaceFaceFeatureData",
    "SurfKnit": "ISurfaceKnitFeatureData", "SurfOffset": "ISurfaceOffsetFeatureData",
    "PlanarSurface": "ISurfacePlanarFeatureData", "FillSurface": "IFillSurfaceFeatureData",
    "SurfTrim": "ISurfaceTrimFeatureData", "SurfExtend": "ISurfaceExtendFeatureData",
    "RuledSrfFromEdge": "IRuledSurfaceFeatureData", "SurfCut": "ISurfaceCutFeatureData",
    "SheetMetal": "ISheetMetalFeatureData", "SMBaseFlange": "IBaseFlangeFeatureData",
    "EdgeFlange": "IEdgeFlangeFeatureData", "Hem": "IHemFeatureData", "Jog": "IJogFeatureData",
    "SketchBend": "ISketchedBendFeatureData", "FlatPattern": "IFlatPatternFeatureData",
    "SMMiteredFlange": "IMiterFlangeFeatureData", "BreakCorner": "IBreakCornerFeatureData",
    "WeldMemberFeat": "IStructuralMemberFeatureData", "EndCap": "IEndCapFeatureData",
    "Gusset": "IGussetFeatureData", "BoundingBox": "IBoundingBoxFeatureData",
    "MateCoincident": "ICoincidentMateFeatureData", "MateConcentric": "IConcentricMateFeatureData",
    "MateDistanceDim": "IDistanceMateFeatureData", "MateParallel": "IParallelMateFeatureData",
    "MatePerpendicular": "IPerpendicularMateFeatureData", "MateTangent": "ITangentMateFeatureData",
    "MatePlanarAngleDim": "IAngleMateFeatureData", "MateGearDim": "IGearMateFeatureData",
    "MateCamTangent": "ICamFollowerMateFeatureData", "MateHinge": "IHingeMateFeatureData",
    "MateScrew": "IScrewMateFeatureData", "MateSlot": "ISlotMateFeatureData", "MateLock": "ILockMateFeatureData",
    "MateRackPinionDim": "IRackPinionMateFeatureData", "MateSymmetric": "ISymmetricMateFeatureData",
    "MateWidth": "IWidthMateFeatureData", "MateProfileCenter": "IProfileCenterMateFeatureData",
    "MateLinearCoupler": "ILinearCouplerMateFeatureData", "MateUniversalJoint": "IUniversalJointMateFeatureData",
}

# swFeatureNameID_e member -> its definition interface, for create_feature.
CREATE_DATA = {
    "swFmChamfer": "IChamferFeatureData2", "swFmFillet": "ISimpleFilletFeatureData2", "swFmDraft": "IDraftFeatureData2",
    "swFmMirrorSolid": "IMirrorSolidFeatureData", "swFmCirPattern": "ICircularPatternFeatureData",
    "swFmLPattern": "ILinearPatternFeatureData", "swFmMirrorPattern": "IMirrorPatternFeatureData",
    "swFmShell": "IShellFeatureData", "swFmBlend": "ILoftFeatureData", "swFmBlendCut": "ILoftFeatureData",
    "swFmExtrusion": "IExtrudeFeatureData2", "swFmBoss": "IExtrudeFeatureData2", "swFmCut": "IExtrudeFeatureData2",
    "swFmRevolution": "IRevolveFeatureData2", "swFmRevCut": "IRevolveFeatureData2", "swFmSweep": "ISweepFeatureData",
    "swFmSweepCut": "ISweepFeatureData", "swFmThicken": "IThickenFeatureData", "swFmThickenCut": "IThickenFeatureData",
    "swFmVarFillet": "IVariableFilletFeatureData2", "swFmHoleWzd": "IWizardHoleFeatureData2",
    "swFmCosmeticThread": "ICosmeticThreadFeatureData", "swFmBaseFlange": "IBaseFlangeFeatureData",
    "swFmSketchBend": "ISketchedBendFeatureData", "swFmEdgeFlange": "IEdgeFlangeFeatureData",
    "swFmRefPlane": "IRefPlaneFeatureData", "swFmRefAxis": "IRefAxisFeatureData",
    "swFmCoordinateSystem": "ICoordinateSystemFeatureData", "swFmWeldMemberFeat": "IStructuralMemberFeatureData",
    "swFmSweepThread": "IThreadFeatureData", "swFmTabAndSlot": "ITabAndSlotFeatureData",
    "swFmCurvePattern": "ICurveDrivenPatternFeatureData", "swFmSketchPattern": "ISketchPatternFeatureData",
    "swFmFillPattern": "IFillPatternFeatureData", "swFmTablePattern": "ITablePatternFeatureData",
    "swFmDimPattern": "IDimPatternFeatureData", "swFmLocalLPattern": "ILocalLinearPatternFeatureData",
    "swFmLocalCirPattern": "ILocalCircularPatternFeatureData", "swFmLocalCurvePattern": "ILocalCurvePatternFeatureData",
    "swFmLocalSketchPattern": "ILocalSketchPatternFeatureData", "swFmBoundingBox": "IBoundingBoxFeatureData",
    "swFmNormalCut": "ISMNormalCutFeatureData2", "swFmMirrorComponent": "IMirrorComponentFeatureData",
    "swFmSweptFlange": "ISweptFlangeFeatureData", "swFmSMGusset": "ISMGussetFeatureData",
    "swFmBeltAndChain": "IBeltChainFeatureData", "swFmCornerRelief": "ICornerReliefFeatureData",
    "swFmMateCoincident": "ICoincidentMateFeatureData", "swFmMateConcentric": "IConcentricMateFeatureData",
    "swFmMateDistanceDim": "IDistanceMateFeatureData", "swFmMateParallel": "IParallelMateFeatureData",
    "swFmMateTangent": "ITangentMateFeatureData", "swFmMatePerpendicular": "IPerpendicularMateFeatureData",
    "swFmMateLock": "ILockMateFeatureData", "swFmMateCamTangent": "ICamFollowerMateFeatureData",
    "swFmMateSlot": "ISlotMateFeatureData", "swFmMatePlanarAngleDim": "IAngleMateFeatureData",
    "swFmMateHinge": "IHingeMateFeatureData", "swFmMateRackPinionDim": "IRackPinionMateFeatureData",
    "swFmMateGearDim": "IGearMateFeatureData", "swFmMateScrew": "IScrewMateFeatureData",
    "swFmMateUniversalJoint": "IUniversalJointMateFeatureData", "swFmMateSymmetric": "ISymmetricMateFeatureData",
    "swFmMateWidth": "IWidthMateFeatureData", "swFmMateProfileCenter": "IProfileCenterMateFeatureData",
    "swFmMateLinearCoupler": "ILinearCouplerMateFeatureData", "swFmSolidToSheetMetal": "IConvertSolidFeatureData",
}

_SW_DOC_PART, _SW_DOC_ASSEMBLY, _SW_DOC_DRAWING = 1, 2, 3
_SEL_FACES, _SEL_EDGES, _SEL_VERTICES = 2, 1, 3


class Core:
    """Generic API access, selection and history control for one session."""

    def __init__(self, session):
        global _SESSION
        _SESSION = session
        self.s = session
        self._handles = {}
        self._checkpoints = {}
        self._watch = {}
        self._journal_pos = {}

    # --- plumbing --------------------------------------------------------------

    @property
    def mod(self):
        return self.s._mod

    def sw(self):
        sw = self.s._ensure()
        enums(sw)
        return sw

    def model(self, write: bool = True):
        self.sw()
        return self.s._require_model() if write else self.s._current_model()

    def ext(self, model=None):
        return binding.wrap((model or self.model()).Extension, self.mod.IModelDocExtension)

    def fm(self, model=None):
        return binding.wrap((model or self.model()).FeatureManager, self.mod.IFeatureManager)

    def skm(self, model=None):
        return binding.wrap((model or self.model()).SketchManager, self.mod.ISketchManager)

    def selmgr(self, model=None):
        return binding.wrap((model or self.model()).SelectionManager, self.mod.ISelectionMgr)

    @contextlib.contextmanager
    def reading(self):
        self.s._reading += 1
        try:
            yield
        finally:
            self.s._reading -= 1

    def iface(self, name: str):
        cls = getattr(self.mod, name, None)
        if cls is None:
            raise SolidWorksError(f"No interface '{name}' in this SolidWorks' type library. Try api_lookup.")
        return cls

    def all_features(self, model=None):
        model = model or self.model(False)
        feat = binding.wrap(model.FirstFeature(), self.mod.IFeature)
        while feat is not None:
            yield feat
            for sub in self.s._sub_features(feat):
                yield sub
            feat = binding.wrap(feat.GetNextFeature(), self.mod.IFeature)

    def feature(self, name: str, model=None):
        model = model or self.model(False)
        kind = int(model.GetType())
        raw = None
        if kind == _SW_DOC_PART:
            raw = binding.wrap(model, self.mod.IPartDoc).FeatureByName(name)
        elif kind == _SW_DOC_ASSEMBLY:
            raw = binding.wrap(model, self.mod.IAssemblyDoc).FeatureByName(name)
        if raw is None:
            for f in self.all_features(model):
                if f.Name == name:
                    return f
            raise SolidWorksError(f"No feature '{name}' in '{model.GetTitle()}'. list_features shows them.")
        return binding.wrap(raw, self.mod.IFeature)

    def rebuild_result(self, model=None, **extra) -> dict:
        model = model or self.model()
        ok = bool(model.ForceRebuild3(False))
        out = {"ok": True, "rebuild_ok": ok, **extra}
        errors = self.feature_errors(model)
        if errors:
            out["feature_errors"] = errors
        if int(model.GetType()) == _SW_DOC_PART:
            try:
                out["mass_properties"] = self.s.get_mass_properties()["mass_properties"]
            except Exception:  # noqa: BLE001 - an empty part has no mass
                pass
        return out

    def feature_errors(self, model=None) -> list:
        model = model or self.model(False)
        out = []
        for f in self.all_features(model):
            try:
                code, warning = f.GetErrorCode2()
            except Exception:  # noqa: BLE001
                continue
            if code:
                out.append({"feature": f.Name, "type": f.GetTypeName2(), "code": code,
                            "error": enum_name("swFeatureError_e", code), "warning": bool(warning)})
        return out

    def last_feature(self, model=None, of_type=None):
        last = None
        for f in self.all_features(model):
            if of_type is None or f.GetTypeName2() in of_type:
                last = f
        return last

    # --- entity resolution and selection -----------------------------------------

    def _bodies(self, model):
        if int(model.GetType()) != _SW_DOC_PART:
            raise SolidWorksError("Face, edge and vertex specs work on parts; in an assembly use component faces.")
        part = binding.wrap(model, self.mod.IPartDoc)
        bodies = part.GetBodies2(0, False) or ()  # swSolidBody
        return [binding.wrap(b, self.mod.IBody2) for b in bodies]

    def _nearest(self, entities, point_mm, wrap_as):
        p = [mm_to_m(c) for c in point_mm]
        best, best_d = None, None
        for e in entities:
            ent = binding.wrap(e, wrap_as)
            try:
                q = ent.GetClosestPointOn(*p)
            except Exception:  # noqa: BLE001
                continue
            if not q or len(q) < 3:
                continue
            d = math.dist(p, q[:3])
            if best_d is None or d < best_d:
                best, best_d = ent, d
        if best is None or best_d > 5e-5:
            raise SolidWorksError(f"Nothing within 0.05 mm of {point_mm} mm (nearest {m_to_mm(best_d or 0):.3f} mm).")
        return best

    def resolve(self, spec, model=None):
        """An entity from a spec. Specs:
        {"face": "+z" | "+z:inner" | "#5"}, {"face_at": [x,y,z]}, {"edge": 5}, {"edge_at": [x,y,z]},
        {"vertex_at": [x,y,z]}, {"named": "Bore_Face"} (name_entity), {"feature": "Boss1"},
        {"plane": "front"|"Plane1"}, {"sketch": "Sketch1"}, {"handle": "h3"} (call_api result)."""
        model = model or self.model()
        if not isinstance(spec, dict):
            raise SolidWorksError(f"An entity spec is an object such as {{'face': '+z'}} (got {spec!r}).")
        if "handle" in spec:
            return self._handle(spec["handle"])
        if "feature" in spec or "sketch" in spec:
            return self.feature(spec.get("feature") or spec.get("sketch"), model)
        if "plane" in spec:
            return self.s._named_plane(spec["plane"])[0]
        if "named" in spec:
            part = binding.wrap(model, self.mod.IPartDoc)
            for kind, cls in ((_SEL_FACES, self.mod.IFace2), (_SEL_EDGES, self.mod.IEdge), (_SEL_VERTICES, self.mod.IVertex)):
                ent = part.GetEntityByName(spec["named"], kind)
                if ent is not None:
                    return binding.wrap(ent, cls)
            raise SolidWorksError(f"No face, edge or vertex named '{spec['named']}'.")
        bodies = self._bodies(model)
        if "face" in spec:
            token = str(spec["face"])
            index = self.s._face_index(token)
            faces = [f for b in bodies for f in self.s._body_faces(b)]
            if index is not None:
                if not 0 <= index < len(self.s._body_faces(bodies[0])):
                    raise SolidWorksError(f"Face index {index} is past the first body's faces (list_faces).")
                return binding.wrap(self.s._body_faces(bodies[0])[index], self.mod.IFace2)
            normal, side = self.s._parse_face_selector(token)
            face = self.s._pick_planar_face(faces, normal, side)[0]
            if face is None:
                raise SolidWorksError(f"No planar face facing {token}.")
            return binding.wrap(face, self.mod.IFace2)
        if "face_at" in spec:
            return self._nearest([f for b in bodies for f in self.s._body_faces(b)], spec["face_at"], self.mod.IFace2)
        if "edge" in spec:
            edges = list(bodies[0].GetEdges() or ())
            i = int(spec["edge"])
            if not 0 <= i < len(edges):
                raise SolidWorksError(f"Edge index {i} is past the {len(edges)} edges (list_edges).")
            return binding.wrap(edges[i], self.mod.IEdge)
        if "edge_at" in spec:
            return self._nearest([e for b in bodies for e in (b.GetEdges() or ())], spec["edge_at"], self.mod.IEdge)
        if "vertex_at" in spec:
            return self._nearest([v for b in bodies for v in (b.GetVertices() or ())], spec["vertex_at"],
                                 self.mod.IVertex)
        raise SolidWorksError(f"Unknown entity spec {spec!r}.")

    def select(self, items, append: bool = False, model=None) -> list:
        """Select entity specs (see resolve) plus {"id": name, "type": "AXIS"} (SelectByID2),
        {"segment": "Line1", "sketch": "Sketch1"} and {"origin": true}; each may carry "mark"."""
        model = model or self.model()
        if not append:
            model.ClearSelection2(True)
        ext = self.ext(model)
        sm = self.selmgr(model)
        done = []
        for i, item in enumerate(items or []):
            mark = int(item.get("mark", 0)) if isinstance(item, dict) else 0
            add = append or i > 0
            if isinstance(item, dict) and "id" in item:
                xyz = [mm_to_m(c) for c in item.get("at_mm", [0, 0, 0])]
                ok = ext.SelectByID2(item["id"], item.get("type", ""), *xyz, add, mark, None, 0)
                label = f"{item['id']} ({item.get('type', '')})"
            elif isinstance(item, dict) and "segment" in item:
                ok = ext.SelectByID2(f"{item['segment']}@{item['sketch']}", "SKETCHSEGMENT", 0, 0, 0, add, mark, None, 0)
                label = item["segment"]
            elif isinstance(item, dict) and "sketch_point" in item:
                ok = ext.SelectByID2(f"{item['sketch_point']}@{item['sketch']}", "SKETCHPOINT", 0, 0, 0, add, mark, None, 0)
                label = item["sketch_point"]
            elif isinstance(item, dict) and item.get("origin"):
                ok = ext.SelectByID2("Point1@Origin", "EXTSKETCHPOINT", 0, 0, 0, add, mark, None, 0)
                label = "origin"
            else:
                ent = self.resolve(item, model)
                label = json.dumps(item)
                if hasattr(ent, "Select2") and hasattr(ent, "GetTypeName2"):
                    ok = ent.Select2(add, mark)
                else:
                    data = binding.wrap(sm.CreateSelectData(), self.mod.ISelectData)
                    data.Mark = mark
                    ok = binding.wrap(ent, self.mod.IEntity).Select4(add, data)
            if not ok:
                raise SolidWorksError(f"Could not select {label}.")
            done.append(label)
        return done

    def select_entities(self, items, append: bool = False) -> dict:
        done = self.select(items, append)
        return {"ok": True, "selected": len(done), "items": done,
                "selection_count": self.selmgr().GetSelectedObjectCount2(-1)}

    def name_entity(self, name: str, entity: dict) -> dict:
        model = self.model()
        part = binding.wrap(model, self.mod.IPartDoc)
        ent = self.resolve(entity, model)
        try:
            part.DeleteEntityName(ent)
        except Exception:  # noqa: BLE001 - had no name
            pass
        if not part.SetEntityName(ent, name):
            raise SolidWorksError(f"SolidWorks refused the name '{name}' (names are unique per part).")
        return {"ok": True, "name": name, "use": {"named": name}}

    # --- generic feature definition access -----------------------------------------

    def _definition(self, feat, interface=None):
        typename = feat.GetTypeName2()
        name = interface or FEATURE_DATA.get(typename)
        if name is None:
            raise SolidWorksError(f"'{feat.Name}' is a {typename}; its definition interface is not mapped. "
                                  "Pass interface='I...FeatureData' (api_lookup FeatureData lists them).")
        raw = feat.GetDefinition()
        if raw is None:
            raise SolidWorksError(f"'{feat.Name}' ({typename}) has no editable definition.")
        return binding.wrap(raw, self.iface(name)), name, typename

    def dump(self, obj, cls) -> dict:
        values = {}
        for prop in sorted(getattr(cls, "_prop_map_get_", {})):
            try:
                values[prop] = jsonable(getattr(obj, prop))
            except Exception:  # noqa: BLE001
                continue
        for name, fn in vars(cls).items():
            if name.startswith("_") or name.startswith("I") or not callable(fn):
                continue
            if not (name.startswith("Get") or name.startswith("Is")):
                continue
            try:
                params = [p for p in inspect.signature(fn).parameters if p != "self"]
            except (TypeError, ValueError):
                continue
            try:
                if not params:
                    values[name + "()"] = jsonable(getattr(obj, name)())
                elif len(params) == 1 and params[0].lower() in ("forward", "fwd", "dir", "direction", "dir1"):
                    values[name + "(True)"] = jsonable(getattr(obj, name)(True))
                    values[name + "(False)"] = jsonable(getattr(obj, name)(False))
            except Exception:  # noqa: BLE001
                continue
        setters = sorted(k for k in getattr(cls, "_prop_map_put_", {}))
        methods = sorted(k for k, v in vars(cls).items() if callable(v) and k.startswith("Set"))
        return {"values": values, "settable_properties": setters, "set_methods": methods}

    def _apply(self, obj, set_values=None, calls=None, model=None) -> list:
        applied = []
        for key, val in (set_values or {}).items():
            setattr(obj, key, self._arg(val, model))
            applied.append(key)
        for call in calls or []:
            method = call["method"]
            args = [self._arg(a, model) for a in call.get("args", [])]
            out = getattr(obj, method)(*args)
            applied.append(f"{method} -> {jsonable(out)}")
        return applied

    def _arg(self, a, model=None):
        if isinstance(a, dict) and "entity" in a:
            return self.resolve(a["entity"], model)
        if isinstance(a, dict) and "entities" in a:
            return dispatch_array([self.resolve(e, model) for e in a["entities"]])
        if isinstance(a, dict) and "doubles" in a:
            return double_array(to_api(a["doubles"]))
        return to_api(a)

    def get_feature(self, name: str, interface: str | None = None) -> dict:
        with self.reading():
            model = self.model(False)
            feat = self.feature(name, model)
            defn, iname, typename = self._definition(feat, interface)
            out = self.dump(defn, self.iface(iname))
            code, warning = feat.GetErrorCode2()
            dims = []
            disp = feat.GetFirstDisplayDimension()
            while disp is not None:
                d = binding.wrap(binding.wrap(disp, self.mod.IDisplayDimension).GetDimension2(0), self.mod.IDimension)
                dims.append({"name": d.GetNameForSelection(), "value_api": d.SystemValue})
                disp = feat.GetNextDisplayDimension(disp)
            return {"ok": True, "feature": feat.Name, "type": typename, "interface": iname,
                    "suppressed": bool(feat.IsSuppressed2(1, None)[0]),
                    "error": enum_name("swFeatureError_e", code) if code else None,
                    "dimensions": dims, **out,
                    "note": "values in API units (m, rad); set with modify_feature"}

    def modify_feature(self, name: str, set: dict | None = None, call: list | None = None,
                       interface: str | None = None) -> dict:
        model = self.model()
        feat = self.feature(name, model)
        defn, iname, _ = self._definition(feat, interface)
        access = getattr(defn, "AccessSelections", None)
        if access is not None:
            access(model, None)
        try:
            applied = self._apply(defn, set, call, model)
        except Exception:
            if hasattr(defn, "ReleaseSelectionAccess"):
                defn.ReleaseSelectionAccess()
            raise
        if not feat.ModifyDefinition(defn, model, None):
            if hasattr(defn, "ReleaseSelectionAccess"):
                defn.ReleaseSelectionAccess()
            raise SolidWorksError(f"SolidWorks refused the new definition of '{name}' ({', '.join(applied)}).")
        return self.rebuild_result(model, feature=feat.Name, applied=applied)

    def create_feature(self, feature_type: str, set: dict | None = None, call: list | None = None,
                       select: list | None = None, name: str | None = None, interface: str | None = None) -> dict:
        model = self.model()
        member = feature_type if feature_type.startswith("swFm") else "swFm" + feature_type
        fid = enum_value(member)
        fm = self.fm(model)
        raw = fm.CreateDefinition(fid)
        if raw is None:
            raise SolidWorksError(f"CreateDefinition({member}) returned nothing: this feature type is not "
                                  "created through definitions. Use call_api with the FeatureManager method.")
        iname = interface or CREATE_DATA.get(member)
        if iname is None:
            raise SolidWorksError(f"No definition interface mapped for {member}; pass interface='I...FeatureData'.")
        data = binding.wrap(raw, self.iface(iname))
        applied = self._apply(data, set, call, model)
        if select:
            self.select(select, False, model)
        feat = fm.CreateFeature(data)
        if feat is None:
            raise SolidWorksError(f"SolidWorks did not create the {member} feature. Check the selections and "
                                  "settings (get_feature on a similar feature shows what a working one holds).")
        feat = binding.wrap(feat, self.mod.IFeature)
        if name:
            feat.Name = name
        model.ClearSelection2(True)
        return self.rebuild_result(model, feature=feat.Name, type=feat.GetTypeName2(), applied=applied)

    # --- reflective API access ------------------------------------------------------

    _TARGETS = {
        "sw": (None, "ISldWorks"), "doc": ("doc", "IModelDoc2"), "ext": ("ext", "IModelDocExtension"),
        "fm": ("fm", "IFeatureManager"), "skm": ("skm", "ISketchManager"), "selmgr": ("selmgr", "ISelectionMgr"),
        "eqmgr": ("eqmgr", "IEquationMgr"), "part": ("doc", "IPartDoc"), "asm": ("doc", "IAssemblyDoc"),
        "drw": ("doc", "IDrawingDoc"), "cfgmgr": ("cfgmgr", "IConfigurationManager"),
        "view": ("view", "IModelView"), "modeler": ("modeler", "IModeler"), "math": ("math", "IMathUtility"),
    }

    def _handle(self, key):
        if key not in self._handles:
            raise SolidWorksError(f"No handle '{key}' (handles last for this server session).")
        return self._handles[key]

    def _target(self, target: str, wrap_as: str | None):
        sw = self.sw()
        if target.startswith("handle:"):
            obj = self._handle(target.split(":", 1)[1])
            return binding.wrap(obj, self.iface(wrap_as)) if wrap_as else obj
        if target.startswith("feature:"):
            return self.feature(target.split(":", 1)[1])
        if target not in self._TARGETS:
            raise SolidWorksError(f"Unknown target '{target}'. Use one of {', '.join(self._TARGETS)}, "
                                  "'feature:<name>' or 'handle:<h>'.")
        kind, iname = self._TARGETS[target]
        if kind is None:
            return sw
        if kind == "modeler":
            return binding.wrap(sw.GetModeler(), self.mod.IModeler)
        if kind == "math":
            return binding.wrap(sw.GetMathUtility(), self.mod.IMathUtility)
        model = self.model()
        raw = {"doc": model, "ext": model.Extension, "fm": model.FeatureManager, "skm": model.SketchManager,
               "selmgr": model.SelectionManager, "eqmgr": model.GetEquationMgr(),
               "cfgmgr": model.ConfigurationManager, "view": model.ActiveView}[kind]
        return binding.wrap(raw, self.iface(wrap_as or iname))

    def call_api(self, target: str, member: str, args: list | None = None, set_value=None,
                 wrap_as: str | None = None, result_as: str | None = None) -> dict:
        obj = self._target(target, wrap_as)
        if set_value is not None:
            setattr(obj, member, self._arg(set_value))
            return {"ok": True, "set": member}
        attr = getattr(obj, member)
        value = attr(*[self._arg(a) for a in (args or [])]) if callable(attr) else attr
        out = {"ok": True}
        if hasattr(value, "_oleobj_") or type(value).__name__ == "PyIDispatch":
            key = f"h{len(self._handles) + 1}"
            self._handles[key] = binding.wrap(value, self.iface(result_as)) if result_as else value
            out.update(handle=key, value=jsonable(self._handles[key]),
                       hint=f"use target 'handle:{key}' with wrap_as='I...' to call its members")
        else:
            out["value"] = jsonable(value)
        return out

    def api_lookup(self, query: str, limit: int = 40) -> dict:
        """Search interfaces, members and enum values of the installed API."""
        self.sw()
        q = query.lower().strip()
        iface_q, _, member_q = q.partition(".") if "." in q else ("", "", q)
        hits = []
        for cname in dir(self.mod):
            if not cname.startswith("I") or (iface_q and iface_q != cname.lower()):
                continue
            cls = getattr(self.mod, cname)
            if not isinstance(cls, type):
                continue
            if not iface_q and member_q in cname.lower() and len(hits) < limit:
                hits.append({"interface": cname})
            for prop in getattr(cls, "_prop_map_get_", {}):
                if member_q in prop.lower():
                    rw = "get/set" if prop in getattr(cls, "_prop_map_put_", {}) else "get"
                    hits.append({"member": f"{cname}.{prop}", "kind": f"property ({rw})"})
            for mname, fn in vars(cls).items():
                if mname.startswith("_") or not callable(fn) or member_q not in mname.lower():
                    continue
                try:
                    params = [p for p in inspect.signature(fn).parameters if p != "self"]
                except (TypeError, ValueError):
                    params = []
                hits.append({"member": f"{cname}.{mname}({', '.join(params)})", "kind": "method"})
            if len(hits) >= limit:
                break
        if len(hits) < limit:
            for ename, members in enums().items():
                if q in ename.lower():
                    hits.append({"enum": ename, "members": members if len(members) <= 40 else f"{len(members)} members"})
                else:
                    for m, v in members.items():
                        if q in m.lower():
                            hits.append({"enum_member": f"{ename}.{m}", "value": v})
                if len(hits) >= limit:
                    break
        return {"ok": True, "count": len(hits[:limit]), "results": hits[:limit],
                "note": "API units are metres and radians; call members with call_api"}

    # --- history control ----------------------------------------------------------

    def checkpoint(self, name: str = "auto", to_file: bool = False) -> dict:
        model = self.model(False)
        if int(model.GetType()) != _SW_DOC_PART:
            raise SolidWorksError("Checkpoints work on parts; save an assembly with save_assembly.")
        names = {f.Name for f in self.s._history()}
        eqmgr = binding.wrap(model.GetEquationMgr(), self.mod.IEquationMgr)
        equations = [eqmgr.Equation(i) for i in range(eqmgr.GetCount())]
        entry = {"title": model.GetTitle(), "features": names, "equations": equations,
                 "time": datetime.datetime.now().isoformat(timespec="seconds")}
        if to_file:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^\w.-]+", "_", f"{model.GetTitle()}-{name}-{stamp}")
            folder = os.path.join(os.path.expanduser("~"), "Documents", "Techne", "snapshots")
            os.makedirs(folder, exist_ok=True)  # SolidWorks will not save into AppData
            path = os.path.join(folder, safe + ".sldprt")
            model.SaveAs3(path, 0, 1 | 2)  # silent | copy: the document keeps its own path
            entry["file"] = path if os.path.exists(path) else None
        self._checkpoints[name] = entry
        return {"ok": True, "checkpoint": name, "features": len(names), "equations": len(equations),
                "file": entry.get("file")}

    def restore_checkpoint(self, name: str = "auto") -> dict:
        if name not in self._checkpoints:
            raise SolidWorksError(f"No checkpoint '{name}'. Taken: {', '.join(self._checkpoints) or 'none'}.")
        entry = self._checkpoints[name]
        model = self.model()
        if model.GetTitle() != entry["title"]:
            raise SolidWorksError(f"Checkpoint '{name}' belongs to '{entry['title']}', not '{model.GetTitle()}'.")
        removed = [f.Name for f in self.s._history() if f.Name not in entry["features"]]
        left = self.s._remove_features_since(entry["features"])
        eqmgr = binding.wrap(model.GetEquationMgr(), self.mod.IEquationMgr)
        dropped = []
        for i in reversed(range(eqmgr.GetCount())):
            text = eqmgr.Equation(i)
            if text not in entry["equations"]:
                eqmgr.Delete(i)
                dropped.append(text)
        return self.rebuild_result(model, restored=name, removed=removed, not_removed=left,
                                   equations_removed=dropped)

    def rollback(self, before: str | None = None, after: str | None = None) -> dict:
        model = self.model()
        fm = self.fm(model)
        if before:
            ok = fm.EditRollback(enum_value("swMoveRollbackBarToBeforeFeature"), before)
        elif after:
            ok = fm.EditRollback(enum_value("swMoveRollbackBarToAfterFeature"), after)
        else:
            ok = fm.EditRollback(enum_value("swMoveRollbackBarToEnd"), "")
        if not ok:
            raise SolidWorksError("SolidWorks would not move the rollback bar there.")
        rolled = [f.Name for f in self.s._history() if f.IsRolledBack()]
        return {"ok": True, "rolled_back": rolled,
                "note": "new features go in at the bar; call rollback() with no arguments to roll forward"}

    def rename_feature(self, name: str, new_name: str) -> dict:
        feat = self.feature(name, self.model())
        feat.Name = new_name
        if feat.Name != new_name:
            raise SolidWorksError(f"SolidWorks kept the name '{feat.Name}' ('{new_name}' may be taken).")
        return {"ok": True, "feature": feat.Name}

    def rebuild_report(self) -> dict:
        model = self.model()
        ok = bool(model.ForceRebuild3(False))
        errors = self.feature_errors(model)
        sketches = self.s._under_defined_sketches() if int(model.GetType()) == _SW_DOC_PART else []
        return {"ok": True, "rebuild_ok": ok, "errors": [e for e in errors if not e["warning"]],
                "warnings": [e for e in errors if e["warning"]], "under_defined_sketches": sketches}

    def feature_stats(self) -> dict:
        with self.reading():
            model = self.model(False)
            stats = binding.wrap(self.fm(model).FeatureStatistics, self.mod.IFeatureStatistics)
            stats.Refresh()
            names, times = stats.FeatureNames or (), stats.FeatureUpdateTimes or ()
            rows = sorted(({"feature": n, "seconds": round(t, 4)} for n, t in zip(names, times)),
                          key=lambda r: -r["seconds"])
            return {"ok": True, "total_rebuild_s": round(stats.TotalRebuildTime, 4), "features": len(rows),
                    "slowest": rows[:15]}

    # --- document data ----------------------------------------------------------

    _UNIT_SYSTEMS = {"mmgs": "swUnitSystem_MMGS", "mks": "swUnitSystem_MKS", "cgs": "swUnitSystem_CGS",
                     "ips": "swUnitSystem_IPS", "custom": "swUnitSystem_Custom"}

    def doc_settings(self, units: str | None = None, standard: str | None = None,
                     decimals: int | None = None) -> dict:
        model = self.model()
        ext = self.ext(model)
        p_units, p_std, p_dec = (enum_value("swUnitSystem"), enum_value("swDetailingDimensionStandard"),
                                 enum_value("swUnitsLinearDecimalPlaces"))
        if units:
            key = self._UNIT_SYSTEMS.get(units.lower())
            if key is None:
                raise SolidWorksError(f"units is one of {', '.join(self._UNIT_SYSTEMS)}.")
            ext.SetUserPreferenceInteger(p_units, 0, enum_value(key))
        if standard:
            ext.SetUserPreferenceInteger(p_std, 0, enum_value("swDetailingStandard" + standard.upper()))
        if decimals is not None:
            ext.SetUserPreferenceInteger(p_dec, 0, int(decimals))
        return {"ok": True,
                "units": enum_name("swUnitSystem_e", ext.GetUserPreferenceInteger(p_units, 0)),
                "drafting_standard": enum_name("swDetailingStandard_e", ext.GetUserPreferenceInteger(p_std, 0)),
                "decimals": ext.GetUserPreferenceInteger(p_dec, 0)}

    def custom_properties(self, set: dict | None = None, delete: list | None = None,
                          configuration: str = "") -> dict:
        model = self.model() if (set or delete) else self.model(False)
        cpm = binding.wrap(self.ext(model).CustomPropertyManager(configuration), self.mod.ICustomPropertyManager)
        text = enum_value("swCustomInfoText")
        replace = enum_value("swCustomPropertyReplaceValue")
        for key, val in (set or {}).items():
            cpm.Add3(key, text, str(val), replace)
        for key in delete or []:
            cpm.Delete2(key)
        props = {}
        for key in cpm.GetNames() or ():
            got = cpm.Get6(key, False)
            # (status, value, resolved, was_resolved, linked)
            props[key] = {"value": got[1], "resolved": got[2]} if got[1] != got[2] else got[1]
        return {"ok": True, "configuration": configuration or "(file)", "properties": props}

    def configurations(self, action: str = "list", name: str | None = None, parent: str | None = None,
                       description: str = "") -> dict:
        model = self.model() if action != "list" else self.model(False)
        if action == "add":
            cfgmgr = binding.wrap(model.ConfigurationManager, self.mod.IConfigurationManager)
            if cfgmgr.AddConfiguration2(name, "", "", 0, parent or "", description, True) is None:
                raise SolidWorksError(f"Could not add configuration '{name}'.")
        elif action == "activate":
            if not model.ShowConfiguration2(name):
                raise SolidWorksError(f"Could not activate configuration '{name}'.")
        elif action == "delete":
            if not model.DeleteConfiguration2(name):
                raise SolidWorksError(f"Could not delete '{name}' (the active configuration cannot be deleted).")
        elif action != "list":
            raise SolidWorksError("action is list, add, activate or delete.")
        active = binding.wrap(model.ConfigurationManager, self.mod.IConfigurationManager).ActiveConfiguration
        return {"ok": True, "configurations": list(model.GetConfigurationNames() or ()),
                "active": binding.wrap(active, self.mod.IConfiguration).Name if active is not None else None}

    # --- awareness ------------------------------------------------------------------

    def watch_changes(self) -> dict:
        """What changed in the current document since the last call: features added,
        removed, renamed or (un)suppressed, equations changed, and the Techne add-in's
        journal of edits made by hand in SolidWorks."""
        with self.reading():
            model = self.model(False)
            title = model.GetTitle()
            now = {}
            for f in self.all_features(model):
                try:
                    now[f.GetID()] = (f.Name, f.GetTypeName2(), bool(f.IsSuppressed2(1, None)[0]))
                except Exception:  # noqa: BLE001
                    continue
            eqmgr = binding.wrap(model.GetEquationMgr(), self.mod.IEquationMgr)
            eqs = {eqmgr.Equation(i): round(eqmgr.Value(i), 9) for i in range(eqmgr.GetCount())} if eqmgr else {}
            before = self._watch.get(title)
            self._watch[title] = (now, eqs)
            out = {"ok": True, "document": title}
            if before is None:
                out["note"] = "first call: baseline taken; the next call reports changes since now"
                out["features"] = len(now)
            else:
                old, old_eqs = before
                out["added"] = [now[i][0] for i in now if i not in old]
                out["removed"] = [old[i][0] for i in old if i not in now]
                out["renamed"] = [f"{old[i][0]} -> {now[i][0]}" for i in now if i in old and old[i][0] != now[i][0]]
                out["suppression_changed"] = [now[i][0] for i in now if i in old and old[i][2] != now[i][2]]
                out["equations_changed"] = [e for e in set(eqs) | set(old_eqs) if eqs.get(e) != old_eqs.get(e)]
            out["journal"] = self._journal(title)
            return out

    def _journal(self, title: str) -> list:
        """New lines from the Techne add-in's journal for this document, if it is installed."""
        safe = re.sub(r"[^\w.-]+", "_", title)
        path = os.path.join(data_dir("journal"), safe + ".jsonl")
        if not os.path.exists(path):
            return []
        pos = self._journal_pos.get(path, 0)
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            fh.seek(pos)
            lines = fh.readlines()
            self._journal_pos[path] = fh.tell()
        out = []
        for line in lines[-200:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    @staticmethod
    def telemetry_summary(last: int = 1000) -> dict:
        path = os.path.join(data_dir(), "telemetry.jsonl")
        if not os.path.exists(path):
            return {"ok": True, "calls": 0}
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.readlines()[-last:]
        stats = {}
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            s = stats.setdefault(e["tool"], {"calls": 0, "failed": 0, "ms": 0.0, "last_error": None})
            s["calls"] += 1
            s["ms"] += e.get("ms", 0)
            if not e.get("ok", True):
                s["failed"] += 1
                s["last_error"] = e.get("error")
        rows = [{"tool": k, "calls": v["calls"], "failed": v["failed"], "avg_ms": round(v["ms"] / v["calls"], 1),
                 "last_error": v["last_error"]} for k, v in stats.items()]
        rows.sort(key=lambda r: (-r["failed"], -r["calls"]))
        return {"ok": True, "calls": len(lines), "tools": rows, "file": path}
