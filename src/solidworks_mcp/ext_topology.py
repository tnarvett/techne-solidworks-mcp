"""Techne topology optimization: SOLIDWORKS Simulation topology studies set up and run
from one call, the result shown back as views the AI can model from, and the modelled
part checked against the same loads and limits.

Workflow:
  1. topology_study(...)   goal, fixtures, loads, limits, manufacturing controls.
                           Runs as a background job (wait=False): poll job_status.
                           topology_sweep(...) runs several mass targets in one job.
  2. topology_views(...)   the material the optimizer keeps, from several views (PNG),
                           each with its screen axes and the part's box in mm.
  3. Model the part from those views (sketches, extrudes, lofts, fillets).
  4. topology_verify(...)  the same fixtures, loads and limits as a static study on the
                           new part: stress, displacement, factor of safety and mass
                           against the targets.

Why comtypes: the topology manager and its controls are dual interfaces whose
IDispatch side answers neither by name nor by the type library's DISPIDs, so pywin32
cannot call them; comtypes calls them through their vtables. Study creation, loads,
meshing and running stay on pywin32, which handles those.

Found on SOLIDWORKS 2025 SP5 (November 2026):
- Constraint EndEdit threw 'the server threw an exception' (RPC_E_SERVERFAULT) far
  more often while the server kept SolidWorks still (CommandInProgress set, feature
  tree off), so the setup runs with both back on (_awake).
- Cause of the random 'the server threw an exception' on constraint EndEdit (live test
  2026-11-14): the comtypes study wrapper was released while its topology manager was in
  use; SolidWorks then frees the study (when it goes idle, so pauses made it worse) and the
  next EndEdit crashes. 4 of 4 setups passed with the study held, 0 of 2 without. Every COM
  object a setup uses is held (_ComTypes.hold) until it ends. A study whose EndEdit threw
  stays unusable, so setup still retries in a new study (up to five) as a safety net.
- A von Mises limit equals FoS = yield / limit (Simulation's FoS constraint compares max
  von Mises with yield), so max_stress_mpa is set as that FoS constraint, which is verified;
  Simulation's own stress constraint is not used.
- Units: swsMassUnits_e (kg = 2), swsLinearUnit_e (mm = 0), swsStrengthUnit_e.
- The goal's default mass constraint is a percentage reduction; max_mass_kg is set as the
  equal percentage (1 - max / design mass), the setup that was verified, rather than by
  switching the constraint to absolute mass.
"""

import contextlib
import glob
import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET

from . import binding
from .errors import SolidWorksError
from .ext_sim import Simulation, _first

_GOALS = {"stiffness": 0, "min_displacement": 1, "min_mass": 2}
_SYMMETRY = {"half": 0, "quarter": 1, "eighth": 2}
_SYMMETRY_PLANES = {"half": 1, "quarter": 2, "eighth": 3}
_DEMOLD = {"two_direction": 0, "pull": 1, "stamping": 2}
_DISP_COMPONENTS = {"ux": 0, "uy": 1, "uz": 2, "ures": 3, "ux_abs": 4, "uy_abs": 5, "uz_abs": 6}
_PRESERVE = {"loads_and_fixtures": 2, "loads": 0, "fixtures": 1, "none": 3}
_KG = 2          # swsMassUnits_e (kept for reference; mass is set as a percentage)
_MM = 0          # swsLinearUnit_e
_LESS, _GREATER = 0, 1   # swsTopologyStudyConstraintComparator_e
_TOPOLOGY = 13   # swsAnalysisStudyTypeTopology_Static
_SERVER_FAULT = "-2147417851"  # RPC_E_SERVERFAULT: 'the server threw an exception

# Screen axes of SolidWorks' standard views, in model axes (+y up): what points right
# and what points up in each picture.
VIEW_AXES = {"front": ("+x", "+y"), "back": ("-x", "+y"), "top": ("+x", "-z"),
             "bottom": ("+x", "+z"), "right": ("-z", "+y"), "left": ("+z", "+y")}

# Error codes from cosworks.tlb (swsTopologyStudy*Errors_e), in words the AI can act on.
_ERR = {
    "manager": {1: "the topology manager is not initialised", 2: "invalid goal", 3: "no goal set",
                4: "needs the min_mass goal", 5: "needs the stiffness goal", 6: "needs the min_displacement goal",
                7: "only one mass constraint", 8: "only one automatic displacement constraint",
                9: "a constraint was not found", 10: "the default constraint cannot be removed",
                11: "too many constraints", 12: "a manufacturing control was not found",
                13: "only one thickness control", 14: "only one demold control", 15: "only one symmetry control",
                16: "only one frequency constraint", 17: "not allowed outside an edit",
                18: "a FoS constraint cannot go with a stress constraint",
                19: "a stress constraint cannot go with a FoS constraint", 20: "only one stress constraint",
                21: "only one factor-of-safety constraint"},
    "mass": {1: "BeginEdit was not called", 2: "invalid value", 3: "the value must be above 0",
             4: "more than 100 %", 5: "more than the part's mass", 6: "invalid unit", 7: "invalid option",
             8: "constraint not found"},
    "fos": {1: "BeginEdit was not called", 2: "invalid value", 3: "the value must be above 0",
            4: "invalid component", 5: "the factor of safety must be between 1 and 10000",
            6: "invalid comparator", 7: "constraint not found", 8: "the material has no valid yield strength"},
    "displacement": {1: "BeginEdit was not called", 2: "invalid value", 3: "the value must be above 0",
                     4: "invalid component", 5: "invalid valuation option", 6: "invalid unit",
                     7: "invalid comparator", 8: "unit not available", 9: "invalid vertex",
                     10: "cannot set the vertex", 11: "invalid location option", 17: "constraint not found"},
    "frequency": {1: "BeginEdit was not called", 2: "invalid value", 3: "the value must be above 0",
                  4: "upper frequency below lower", 5: "modes not in ascending order", 6: "give a range",
                  7: "arrays of different length", 8: "invalid mode data", 9: "invalid comparator",
                  10: "invalid frequency values", 15: "constraint not found"},
    "preserved": {1: "BeginEdit was not called", 2: "invalid face count", 3: "invalid depth",
                  4: "invalid depth unit", 5: "invalid face array", 6: "those are not faces of the part"},
    "symmetry": {1: "BeginEdit was not called", 2: "that is not a plane",
                 3: "invalid symmetry type",
                 4: "wrong number of planes for the type (half 1, quarter 2, eighth 3)",
                 5: "type must be set before planes"},
    "thickness": {1: "BeginEdit was not called", 2: "invalid thickness", 3: "invalid thickness unit"},
    "demold": {1: "BeginEdit was not called", 2: "that is not an edge", 3: "invalid direction option",
               4: "that is not a plane", 5: "not available for this direction", 6: "plane not available",
               7: "this direction needs a plane", 8: "edge not available", 9: "this direction needs an edge"},
}

_CT = None


def _split(ret):
    """comtypes gives (error code, object) for Create*/Get* calls with an [out] error code."""
    if not isinstance(ret, tuple):
        return (ret, 0) if not isinstance(ret, int) else (None, ret)
    obj = next((x for x in ret if x is not None and not isinstance(x, (int, float, str))), None)
    err = next((x for x in ret if isinstance(x, int)), 0)
    return obj, err


def _code(ret) -> int:
    if isinstance(ret, tuple):
        ret = next((x for x in reversed(ret) if isinstance(x, int)), 0)
    return ret if isinstance(ret, int) else 0


def _check(ret, what, table):
    code = _code(ret)
    if code:
        raise SolidWorksError(f"Simulation refused the {what}: {table.get(code, f'error code {code}')}.")


def _study_name(name) -> str:
    """Simulation refuses names with dots and some other characters (code 2)."""
    clean = re.sub(r"[^A-Za-z0-9_\- ]+", "_", str(name or "")).strip(" _")
    return (clean or "Topology_Techne")[:60]


def _techne_dir(*parts) -> str:
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(root, "Techne", *parts)
    os.makedirs(path, exist_ok=True)
    return path


def yield_from_sldmat(paths, material) -> float | None:
    """Yield strength (MPa) of `material` from SolidWorks material files: the SIGYLD
    property of the <material name=...> element, in Pa. Pure; unit-tested."""
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        for el in root.iter():
            if el.tag.rsplit("}", 1)[-1] != "material" or el.get("name") != material:
                continue
            for prop in el.iter():
                if prop.tag.rsplit("}", 1)[-1] == "SIGYLD" and prop.get("value"):
                    try:
                        return float(prop.get("value")) / 1e6
                    except ValueError:
                        return None
    return None


def stress_to_fos(max_stress_mpa, yield_mpa, min_fos=None) -> float:
    """The factor-of-safety limit that equals a von Mises limit; with an explicit FoS
    limit too, the stricter one. Pure; unit-tested."""
    fos = None
    if max_stress_mpa is not None:
        if max_stress_mpa <= 0:
            raise SolidWorksError("max_stress_mpa must be above 0.")
        if not yield_mpa:
            raise SolidWorksError("The material has no yield strength, so a stress limit cannot be set: "
                                  "assign a material with one (material='6061-T6 (SS)', '7075-T6 (SN)', "
                                  "'AISI 304', ...).")
        fos = yield_mpa / max_stress_mpa
        if fos < 1.0:
            raise SolidWorksError(f"max_stress_mpa {max_stress_mpa} is above the material's yield strength "
                                  f"({yield_mpa:.1f} MPa): the part would yield. Lower the limit or pick a "
                                  "stronger material.")
    if min_fos is not None:
        if min_fos < 1.0:
            raise SolidWorksError("min_factor_of_safety must be at least 1.")
        fos = max(fos or 0.0, float(min_fos))
    return fos


def mass_target(design_kg, reduction_percent=None, max_kg=None) -> float | None:
    """Mass the optimised part should come to. Pure; unit-tested."""
    if max_kg is not None:
        return float(max_kg)
    if reduction_percent is not None and design_kg:
        return design_kg * (1.0 - float(reduction_percent) / 100.0)
    return None


def _raw(obj):
    """The plain COM pointer inside a comtypes late-bound wrapper. Methods typed to return
    IDispatch (IPartDoc.FeatureByName) give such a wrapper; passed on as an argument, ctypes asks
    it for '_as_parameter_', which it forwards to IDispatch and gets 'Unknown name'."""
    inner = obj.__dict__.get("_comobj") if hasattr(obj, "__dict__") else None
    return inner if inner is not None else obj


class _SetupFailed(Exception):
    """Simulation threw during the setup: the study is unusable, try a new one."""

    def __init__(self, step, steps):
        super().__init__(step)
        self.step, self.steps = step, steps


class _ComTypes:
    """vtable access to SolidWorks and Simulation through comtypes."""

    def __init__(self, exe_dir):
        try:
            import comtypes.client as cc
        except ImportError as exc:
            raise SolidWorksError("Topology studies need the 'comtypes' package. Run install.ps1 again "
                                  "(it installs the server with comtypes).") from exc
        self.cc = cc
        self.cw = cc.GetModule(os.path.join(exe_dir, "Simulation", "cosworks.tlb"))
        self.swt = cc.GetModule(os.path.join(exe_dir, "sldworks.tlb"))
        self.keep = []

    def hold(self, *objs):
        """Keep COM objects alive until release(). A topology manager whose study wrapper was
        released crashed on its next EndEdit (live test 2026-11-14: 4 of 4 setups passed with the
        study held, 0 of 2 without), so nothing an operation uses is let go before it ends."""
        self.keep.extend(o for o in objs if o is not None)
        return objs[-1] if objs else None

    def release(self):
        self.keep = []

    def app(self):
        return self.hold(self.cc.GetActiveObject("SldWorks.Application", interface=self.swt.ISldWorks))

    def study(self, name):
        sw = self.app()
        addin = sw.GetAddInObject("SldWorks.Simulation")
        if addin is None:
            raise SolidWorksError("The SOLIDWORKS Simulation add-in is not loaded.")
        cosmos = addin.QueryInterface(self.cw.ICwAddincallback).CosmosWorks
        doc = cosmos.ActiveDoc
        mgr = doc.StudyManager
        self.hold(addin, cosmos, doc, mgr)
        for i in range(mgr.StudyCount):
            st = mgr.GetStudy(i)
            st = st[0] if isinstance(st, tuple) else st
            if st is not None and st.Name == name:
                return self.hold(st)
        raise SolidWorksError(f"Simulation has no study '{name}'.")

    @staticmethod
    def _first(ret):
        """IEnum*.Next(1) answers ([item], fetched) or similar: the item, or None at the end."""
        while isinstance(ret, (tuple, list)):
            if not ret:
                return None
            ret = ret[0]
        return ret or None

    def _faces(self):
        """Every face of the active part, walked with SolidWorks' enumerators: this comtypes cannot
        read the VARIANT arrays of objects that GetBodies2 / GetFaces return (typecode 0x2009)."""
        sw = self.app()
        part = self.hold(sw.ActiveDoc.QueryInterface(self.swt.IPartDoc))
        bodies = self.hold(part.EnumBodies3(0, True))   # solid bodies, visible
        faces = []
        while True:
            body = self._first(bodies.Next(1))
            if body is None:
                break
            body = self.hold(body.QueryInterface(self.swt.IBody2))
            face = body.GetFirstFace()
            while face:
                face = self.hold(face.QueryInterface(self.swt.IFace2))
                faces.append(face)
                face = face.GetNextFace()
        if not faces:
            raise SolidWorksError("The part has no faces to pick.")
        return faces

    def faces_matching(self, py_faces):
        """The comtypes faces that are the given pywin32 faces (matched on area and centre)."""
        candidates = []
        for face in self._faces():
            box = face.GetBox()
            candidates.append((face, face.GetArea(), [(box[i] + box[i + 3]) / 2.0 for i in range(3)]))
        out = []
        for pf in py_faces:
            box = pf.GetBox()
            centre = [(box[i] + box[i + 3]) / 2.0 for i in range(3)]
            area = pf.GetArea()
            best = min(candidates, key=lambda c: abs(c[1] - area) / max(area, 1e-12) + math.dist(c[2], centre) * 1e3)
            out.append(best[0])
        return out

    def edges_matching(self, py_edges):
        """The comtypes edges that are the given pywin32 edges (matched on their end points)."""
        candidates = []
        for face in self._faces():
            walk = self.hold(face.EnumEdges())
            while True:
                edge = self._first(walk.Next(1))
                if edge is None:
                    break
                edge = self.hold(edge.QueryInterface(self.swt.IEdge))
                p = list(edge.GetCurveParams2())[:6]
                candidates.append((edge, p[:3], p[3:6]))
        if not candidates:
            raise SolidWorksError("The part has no edges to pick.")
        out = []
        for pe in py_edges:
            p = list(pe.GetCurveParams2())[:6]
            a, b = p[:3], p[3:6]
            best = min(candidates, key=lambda c: min(math.dist(c[1], a) + math.dist(c[2], b),
                                                     math.dist(c[1], b) + math.dist(c[2], a)))
            out.append(best[0])
        return out

    def plane(self, name):
        sw = self.app()
        model = sw.ActiveDoc.QueryInterface(self.swt.IModelDoc2)
        part = self.hold(model, model.QueryInterface(self.swt.IPartDoc))
        feat = part.FeatureByName(name)
        if feat is None:
            raise SolidWorksError(f"No plane '{name}'.")
        return self.hold(feat, _raw(feat))


class Topology(Simulation):
    def _ct(self):
        global _CT
        if _CT is None:
            _CT = _ComTypes(self.c.sw().GetExecutablePath())
        return _CT

    def _ct_faces(self, specs):
        return self._ct().faces_matching([self.c.resolve(f) for f in specs])

    def _ct_edges(self, specs):
        return self._ct().edges_matching([self.c.resolve(e) for e in specs])

    def _ct_plane(self, spec):
        if "plane" in spec:
            plane = self.s._named_plane(spec["plane"])[0]
            return self._ct().plane(plane.Name)
        return self._ct_faces([spec])[0]

    # ------------------------------------------------------------------ helpers

    @contextlib.contextmanager
    def _awake(self):
        """SolidWorks drawing and the feature tree back on while Simulation edits a study
        (the server keeps them off during tool calls; see the module notes)."""
        s = self.s
        depth, s._quiet_depth = s._quiet_depth, 0
        s._wake_all()
        busy = s._command_in_progress(False)
        try:
            yield
        finally:
            s._command_in_progress(busy)
            s._quiet_depth = depth

    @staticmethod
    def _settle(seconds=0.3):
        import pythoncom
        end = time.time() + seconds
        while time.time() < end:
            pythoncom.PumpWaitingMessages()
            time.sleep(0.05)

    def _study_names(self, cw, doc) -> list:
        mgr = binding.wrap(doc.StudyManager, cw.ICWStudyManager)
        names = []
        for i in range(mgr.StudyCount):
            st = binding.wrap(_first(mgr.GetStudy(i)), cw.ICWStudy)
            if st is not None:
                names.append(st.Name)
        return names

    def _free_name(self, cw, doc, base) -> str:
        names = set(self._study_names(cw, doc))
        if base not in names:
            return base
        n = 2
        while f"{base}_{n}" in names:
            n += 1
        return f"{base}_{n}"

    def _delete_studies(self, cw, doc, names) -> list:
        mgr = binding.wrap(doc.StudyManager, cw.ICWStudyManager)
        left = []
        for name in names:
            try:
                mgr.DeleteStudy(name)
            except Exception:  # noqa: BLE001 - reported, not fatal
                left.append(name)
        return left

    def _yield_mpa(self, cw, st) -> float | None:
        """Yield strength (MPa) of the part's material, from the SolidWorks material file it comes
        from (Simulation's own ICWMaterial.GetPropertyByName refuses pywin32's call, 2025 SP5)."""
        try:
            from .ext_core import enum_value
            model = self.s._require_model()
            part = binding.wrap(model, self.s._mod.IPartDoc)
            ret = part.GetMaterialPropertyName2("")
            name, db = (ret[0], ret[1]) if isinstance(ret, tuple) else (ret, "")
            if not name:
                return None
            sw = self.c.sw()
            folders = [d for d in (sw.GetUserPreferenceStringValue(enum_value("swFileLocationsMaterialDatabases"))
                                   or "").split(";") if d]
            folders += glob.glob(os.path.join(sw.GetExecutablePath(), "lang", "*", "sldmaterials"))
            files = [f for d in folders for f in glob.glob(os.path.join(d, "*.sldmat"))]
            # the database the part names first, then the rest
            files.sort(key=lambda f: os.path.splitext(os.path.basename(f))[0].lower() != str(db).lower())
            return yield_from_sldmat(files, name)
        except Exception:  # noqa: BLE001
            return None

    def _begin(self, tsm, tries=10):
        last = None
        for _attempt in range(tries):
            try:
                tsm.BeginEdit()
                return tsm
            except Exception as exc:  # noqa: BLE001 - the study is still settling
                last = exc
                time.sleep(0.5)
        raise _SetupFailed("open the study for editing", [["BeginEdit", f"threw {last}"]])

    def _topology_manager(self, cw, doc, study, goal_code):
        """The study's topology manager, open for editing. Found by trial on SOLIDWORKS 2025:
        the manager answers only after a first call outside an edit (it returns 17, 'not
        supported', and initialises), and BeginEdit needs the study to be the active one."""
        cst = self._ct().study(study)   # held by _ComTypes until the setup ends
        tsm = self._ct().hold(cst.TopologyStudyManager)
        tsm.CreateGoal(goal_code)
        tsm.CreateMassConstraint()
        mgr = binding.wrap(doc.StudyManager, cw.ICWStudyManager)
        for i in range(mgr.StudyCount):
            got = _first(mgr.GetStudy(i))
            if got is not None and binding.wrap(got, cw.ICWStudy).Name == study:
                mgr.ActiveStudy = i
                break
        return self._begin(tsm)

    def _default_constraint(self, tsm, getter, names):
        for name in names:
            obj, err = _split(getattr(tsm, getter)(name))
            if obj is not None and not err:
                return self._ct().hold(obj)
        return None

    def _mass_constraint(self, tsm):
        """The goal's default mass constraint ('Mass Constraint 1'), else a new one."""
        obj = self._default_constraint(tsm, "GetMassConstraint",
                                       ("Mass Constraint 1", "Mass Constraint1", "Mass Constraint"))
        if obj is not None:
            return obj
        obj, err = _split(tsm.CreateMassConstraint())
        if obj is None or err:
            raise SolidWorksError(f"Could not find or create the mass constraint: "
                                  f"{_ERR['manager'].get(err, f'error code {err}')}.")
        return obj

    def _displacement_constraint(self, tsm, goal):
        if goal == "min_mass":  # that goal comes with its own displacement constraint
            obj = self._default_constraint(tsm, "GetDisplacementConstraint",
                                           ("Displacement Constraint 1", "Displacement Constraint1",
                                            "Displacement Constraint"))
            if obj is not None:
                return obj
        obj, err = _split(tsm.CreateDisplacementConstraint())
        if obj is None or err:
            raise SolidWorksError(f"Could not create the displacement constraint: "
                                  f"{_ERR['manager'].get(err, f'error code {err}')}.")
        return obj

    def _created(self, ret, what):
        obj, err = _split(ret)
        if obj is None or err:
            raise SolidWorksError(f"Could not create the {what}: {_ERR['manager'].get(err, f'error code {err}')}.")
        return self._ct().hold(obj)

    @staticmethod
    def _abandon(tsm):
        try:
            tsm.EndEdit()
        except Exception:  # noqa: BLE001 - the study is being given up anyway
            pass

    # ------------------------------------------------------------------ records

    def _record_path(self, part_title, study) -> str:
        safe = re.sub(r"[^\w\- ]+", "_", f"{part_title}__{study}")
        return os.path.join(_techne_dir("topology"), safe + ".json")

    def _recall(self, study, source_part=None) -> dict:
        folder = _techne_dir("topology")
        tail = "__" + re.sub(r"[^\w\- ]+", "_", study) + ".json"
        found = [p for p in glob.glob(os.path.join(folder, "*.json")) if p.endswith(tail)]
        if source_part:
            head = re.sub(r"[^\w\- ]+", "_", source_part) + "__"
            found = [p for p in found if os.path.basename(p).startswith(head)]
        if not found:
            raise SolidWorksError(f"No topology setup recorded for study '{study}'"
                                  + (f" on '{source_part}'" if source_part else "")
                                  + ". Pass fixtures and loads to topology_verify instead.")
        with open(max(found, key=os.path.getmtime), encoding="utf-8") as fh:
            return json.load(fh)

    # ------------------------------------------------------------------ setup

    def topology_study(self, fixtures: list, loads: list, goal: str = "stiffness",
                       mass_reduction_percent: float | None = None, max_mass_kg: float | None = None,
                       max_stress_mpa: float | None = None, min_factor_of_safety: float | None = None,
                       max_displacement_mm: float | None = None, min_frequency_hz: float | None = None,
                       preserved: list | None = None, preserved_depth_mm: float = 0.0,
                       symmetry: dict | None = None, min_thickness_mm: float | None = None,
                       max_thickness_mm: float | None = None, demold: dict | None = None,
                       study: str = "Topology_Techne", material: str | None = None,
                       element_size_mm: float | None = None, max_iterations: int | None = None,
                       run: bool = True, displacement_component: str = "ures",
                       preserve_load_faces: bool = True, replace: bool = False) -> dict:
        if goal not in _GOALS:
            raise SolidWorksError(f"goal is one of {', '.join(_GOALS)}.")
        if not fixtures or not loads:
            raise SolidWorksError("A topology study needs fixtures and loads (as sim_static).")
        notes = []
        if goal == "min_mass":
            if mass_reduction_percent is not None or max_mass_kg is not None:
                raise SolidWorksError("min_mass minimises the mass itself: give max_displacement_mm, "
                                      "max_stress_mpa / min_factor_of_safety or min_frequency_hz, no mass target.")
            if max_displacement_mm is None and max_stress_mpa is None and min_factor_of_safety is None \
                    and min_frequency_hz is None:
                raise SolidWorksError("min_mass needs a limit to hold: max_displacement_mm, max_stress_mpa, "
                                      "min_factor_of_safety or min_frequency_hz.")
        elif mass_reduction_percent is None and max_mass_kg is None:
            mass_reduction_percent = 50.0
            notes.append("no mass target given: reducing mass by 50 % (SolidWorks' default)")
        if mass_reduction_percent is not None and not 0 < float(mass_reduction_percent) < 100:
            raise SolidWorksError("mass_reduction_percent is between 0 and 100.")
        if displacement_component.lower() not in _DISP_COMPONENTS:
            raise SolidWorksError(f"displacement_component is one of {', '.join(_DISP_COMPONENTS)}.")
        if symmetry:
            kind = symmetry.get("type", "half")
            if kind not in _SYMMETRY:
                raise SolidWorksError(f"symmetry type is one of {', '.join(_SYMMETRY)}.")
            if len(symmetry.get("planes", [])) != _SYMMETRY_PLANES[kind]:
                raise SolidWorksError(f"{kind} symmetry takes {_SYMMETRY_PLANES[kind]} plane(s).")
        if demold:
            if demold.get("type", "two_direction") not in _DEMOLD:
                raise SolidWorksError(f"demold type is one of {', '.join(_DEMOLD)}.")
            if "edge" not in demold:
                raise SolidWorksError("demold needs an edge along the pull direction: {\"edge\": {\"edge_at\": "
                                      "[x, y, z]}} (a straight edge parallel to the pull); two_direction may add "
                                      "a central \"plane\", else it is found automatically.")

        if material:
            self.s.set_material(material)
        title = self.s._require_model().GetTitle()
        design_kg = self.s.get_mass_properties()["mass_properties"]["mass_kg"]
        if max_mass_kg is not None and not 0 < float(max_mass_kg) < design_kg:
            raise SolidWorksError(f"max_mass_kg must be between 0 and the design space's mass ({design_kg:.4g} kg).")
        plan = {"goal": goal, "goal_code": _GOALS[goal], "design_kg": design_kg,
                "mass_reduction_percent": mass_reduction_percent,
                "max_mass_kg": max_mass_kg, "max_displacement_mm": max_displacement_mm,
                "displacement_component": displacement_component.lower(), "min_frequency_hz": min_frequency_hz,
                "preserved": preserved, "preserved_depth_mm": preserved_depth_mm, "symmetry": symmetry,
                "min_thickness_mm": min_thickness_mm, "max_thickness_mm": max_thickness_mm, "demold": demold}

        yield_mpa = None
        if max_stress_mpa is not None:
            yield_mpa = self._yield_mpa(None, None)
        plan["fos"] = stress_to_fos(max_stress_mpa, yield_mpa, min_factor_of_safety) \
            if (max_stress_mpa is not None or min_factor_of_safety is not None) else None

        self._ct().release()
        with self._awake():
            cw, doc = self._cosmos()
            target = _study_name(study)
            before = self._study_names(cw, doc)
            attempts, broken, applied, work = [], [], None, None
            for tactic in ("direct", "direct", "commit_goal", "direct", "commit_goal"):
                name = target if target not in before and not attempts else self._free_name(cw, doc, target)
                st = binding.wrap(_first(binding.wrap(doc.StudyManager, cw.ICWStudyManager)
                                         .CreateNewStudy3(name, _TOPOLOGY, 0)), cw.ICWStudy)
                if st is None:
                    raise SolidWorksError("Could not create the topology study: it needs Simulation "
                                          "Professional or Premium.")
                try:
                    self._apply_loads(cw, st, fixtures, loads)
                    applied = self._configure(cw, doc, name, tactic, plan)
                    work = name
                    break
                except _SetupFailed as fail:
                    attempts.append({"study": name, "tactic": tactic, "failed_at": fail.step, "steps": fail.steps})
                    # gone before the next try: a study left open in an edit spoiled the next ones
                    broken += self._delete_studies(cw, doc, [name])
                except Exception:
                    self._delete_studies(cw, doc, [name] + broken)
                    raise
            if work is None:
                left = self._delete_studies(cw, doc, broken)
                raise SolidWorksError(
                    "Simulation threw while taking the topology setup, five times in new studies. Save the "
                    "part, restart SolidWorks if it keeps happening, and try again. Details: "
                    + json.dumps(attempts) + (f" Could not delete: {', '.join(left)}." if left else ""))
            leftovers = self._delete_studies(cw, doc, broken)
            if replace and target in before and work != target:
                self._delete_studies(cw, doc, [target])
                mgr = binding.wrap(doc.StudyManager, cw.ICWStudyManager)
                try:
                    if not _code(mgr.RenameStudyFromName(work, target)):
                        work = target
                except Exception:  # noqa: BLE001 - keeps its own name
                    notes.append(f"could not rename '{work}' to '{target}'")
            elif work != target:
                notes.append(f"'{target}' exists, so this study is '{work}' (replace=True swaps it in)")
            notes += self._set_options(work, preserve_load_faces, max_iterations)
            self._ct().release()

        if max_stress_mpa is not None:
            why = f"von Mises < {max_stress_mpa} MPa on yield {yield_mpa:.1f} MPa"
            if min_factor_of_safety is not None:
                why += f", min_factor_of_safety {min_factor_of_safety}; the stricter one"
            applied = [f"{a} ({why})" if a.startswith("FoS") else a for a in applied]
        record = {"study": work, "part": title, "goal": goal, "fixtures": fixtures, "loads": loads,
                  "material": material, "design_space_mass_kg": design_kg, "yield_mpa": yield_mpa,
                  "mass_reduction_percent": mass_reduction_percent, "max_mass_kg": max_mass_kg,
                  "limits": {"max_stress_mpa": max_stress_mpa, "min_factor_of_safety": min_factor_of_safety,
                             "max_displacement_mm": max_displacement_mm, "min_frequency_hz": min_frequency_hz},
                  "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with open(self._record_path(title, work), "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=1)
        out = {"ok": True, "study": work, "applied": applied, "design_space_mass_kg": round(design_kg, 6),
               "target_mass_kg": mass_target(design_kg, mass_reduction_percent, max_mass_kg)}
        if notes:
            out["notes"] = notes
        if attempts:
            out["retried"] = attempts
        if leftovers:
            out["could_not_delete"] = leftovers
        if not run:
            out["next"] = "topology_run to mesh and solve"
            return out
        out.update(self.topology_run(work, element_size_mm))
        return out

    def _configure(self, cw, doc, study, tactic, plan) -> list:
        """Goal, constraints and manufacturing controls on a study with its loads on.
        Raises _SetupFailed when Simulation throws (try a new study), SolidWorksError when it
        refuses a value (the caller's input)."""
        tsm = self._topology_manager(cw, doc, study, plan["goal_code"])
        steps, applied = [], []

        def do(label, fn):
            try:
                result = fn()
            except SolidWorksError:
                self._abandon(tsm)
                raise
            except _SetupFailed:
                raise
            except Exception as exc:  # noqa: BLE001
                steps.append([label, f"threw {exc}"])
                self._abandon(tsm)
                if _SERVER_FAULT in str(exc):  # Simulation threw inside: this study is spent, try anew
                    raise _SetupFailed(label, steps) from exc
                raise SolidWorksError(f"Topology setup failed at the {label}: {exc}") from exc
            steps.append([label, "ok"])
            return result

        goal = plan["goal"]
        do("goal", lambda: _check(tsm.CreateGoal(plan["goal_code"]), f"goal '{goal}'", _ERR["manager"]))
        applied.append(f"goal {goal}")
        if tactic == "commit_goal":
            do("commit goal", lambda: _check(tsm.EndEdit(), "goal", _ERR["manager"]))
            do("reopen", lambda: self._begin(tsm))

        pct, kg = plan["mass_reduction_percent"], plan["max_mass_kg"]
        if pct is None and kg is not None:  # as the equal percentage (see below)
            pct = 100.0 * (1.0 - float(kg) / plan["design_kg"])
        if pct is not None:
            def mass():
                # the goal's default constraint is already 'reduce mass by %': only the value is set
                c = self._mass_constraint(tsm)
                c.BeginEdit()
                _check(c.SetValue(float(pct)), "mass constraint", _ERR["mass"])
                _check(c.EndEdit(), "mass constraint", _ERR["mass"])
            do("mass constraint", mass)
            applied.append(f"reduce mass by {pct:.4g} %" + (f" (mass below {kg} kg)" if kg is not None else ""))

        if plan.get("fos") is not None:
            fos = plan["fos"]

            def fos_constraint():
                c = self._created(tsm.CreateFactorOfSafetyConstraint(), "factor of safety constraint")
                c.BeginEdit()
                _check(c.SetComponent(0), "factor of safety constraint", _ERR["fos"])   # max von Mises
                _check(c.SetComparator(_GREATER), "factor of safety constraint", _ERR["fos"])
                _check(c.SetValue(float(fos)), "factor of safety constraint", _ERR["fos"])
                _check(c.EndEdit(), "factor of safety constraint", _ERR["fos"])
            do("factor of safety constraint", fos_constraint)
            applied.append(f"FoS > {fos:.3g}")

        if plan["max_displacement_mm"] is not None:
            comp = plan["displacement_component"]

            def displacement():
                c = self._displacement_constraint(tsm, goal)
                c.BeginEdit()
                e = _ERR["displacement"]
                _check(c.SetComponent(_DISP_COMPONENTS[comp]), "displacement constraint", e)
                _check(c.SetComparator(_LESS), "displacement constraint", e)
                _check(c.SetValuationPreference(0), "displacement constraint", e)   # absolute value
                _check(c.SetLocationPreference(0), "displacement constraint", e)    # automatic: max point
                _check(c.SetUnit(_MM), "displacement constraint", e)
                _check(c.SetValue(float(plan["max_displacement_mm"])), "displacement constraint", e)
                _check(c.EndEdit(), "displacement constraint", e)
            do("displacement constraint", displacement)
            applied.append(f"{comp.upper()} < {plan['max_displacement_mm']} mm")

        if plan["min_frequency_hz"] is not None:
            def frequency():
                c = self._created(tsm.CreateFrequencyConstraint(), "frequency constraint")
                c.BeginEdit()
                c.SetModeTrackingFlag(True)
                # values are text: one number, or a range "200-400" for 'is between' (API remarks);
                # numbers were refused with code 10 (live test)
                _check(c.SetFrequencyData([1], [_GREATER], [f"{float(plan['min_frequency_hz']):g}"]),
                       "frequency constraint", _ERR["frequency"])
                _check(c.EndEdit(), "frequency constraint", _ERR["frequency"])
            do("frequency constraint", frequency)
            applied.append(f"mode 1 > {plan['min_frequency_hz']} Hz")

        if plan["preserved"]:
            def preserved():
                c = self._created(tsm.CreatePreservedRegionControl(), "preserved region")
                c.BeginEdit()
                self.c.select(plan["preserved"])  # Simulation reads the selection too
                _check(c.SelectFaces(self._ct_faces(plan["preserved"])), "preserved region", _ERR["preserved"])
                if plan["preserved_depth_mm"]:
                    c.SetIncludeRegionDepth(True)
                    c.SetAreaDepthUnit(_MM)
                    c.SetAreaDepth(float(plan["preserved_depth_mm"]))
                _check(c.EndEdit(), "preserved region", _ERR["preserved"])
            do("preserved region", preserved)
            applied.append(f"{len(plan['preserved'])} preserved face(s)"
                           + (f", {plan['preserved_depth_mm']} mm deep" if plan["preserved_depth_mm"] else ""))

        if plan["symmetry"]:
            kind = plan["symmetry"].get("type", "half")

            def symmetry():
                c = self._created(tsm.CreateSymmetryControl(), "symmetry control")
                c.BeginEdit()
                c.SelectSymmetryType(_SYMMETRY[kind])
                planes = plan["symmetry"]["planes"]
                # Simulation also reads SolidWorks' selection: passing the planes alone gave error 4
                # (wrong plane count); with them selected as well, EndEdit returned 0 (live test).
                self.c.select(planes)
                for setter, spec in zip(("SelectFirstSymmetryPlane", "SelectSecondSymmetryPlane",
                                         "SelectThirdSymmetryPlane"), planes):
                    getattr(c, setter)(self._ct_plane(spec))
                _check(c.EndEdit(), "symmetry control", _ERR["symmetry"])
                self.s._require_model().ClearSelection2(True)
            do("symmetry control", symmetry)
            applied.append(f"{kind} symmetry")

        if plan["min_thickness_mm"] or plan["max_thickness_mm"]:
            def thickness():
                c = self._created(tsm.CreateThicknessControl(), "thickness control")
                c.BeginEdit()
                if plan["min_thickness_mm"]:
                    c.SetIncludeMinMemberThickness(True)
                    c.SetMinimumMemberThicknessUnit(_MM)
                    c.SetMinimumMemberThickness(float(plan["min_thickness_mm"]))
                if plan["max_thickness_mm"]:
                    c.SetIncludeMaxMemberThickness(True)
                    c.SetMaxMemberThicknessUnit(_MM)
                    c.SetMaxMemberThickness(float(plan["max_thickness_mm"]))
                _check(c.EndEdit(), "thickness control", _ERR["thickness"])
            do("thickness control", thickness)
            applied.append("member thickness"
                           + (f" >= {plan['min_thickness_mm']} mm" if plan["min_thickness_mm"] else "")
                           + (f" <= {plan['max_thickness_mm']} mm" if plan["max_thickness_mm"] else ""))

        if plan["demold"]:
            kind = plan["demold"].get("type", "two_direction")

            def demold():
                spec = plan["demold"]
                c = self._created(tsm.CreateDemoldControl(), "demold control")
                c.BeginEdit()
                c.SelectDemoldDirection(_DEMOLD[kind])
                picks = [spec["edge"]] + ([spec["plane"]] if "plane" in spec else [])
                self.c.select(picks)  # Simulation reads SolidWorks' selection too
                c.SelectEdgeForPullDirection(self._ct_edges([spec["edge"]])[0])
                if "plane" in spec:  # the automatic mid-plane is on by default ('plane not available')
                    c.SetAutoDetermineCentralMidPlane(False)
                    c.SelectPlaneForDirection(self._ct_plane(spec["plane"]))
                elif kind == "two_direction":
                    c.SetAutoDetermineCentralMidPlane(True)
                if spec.get("reverse"):
                    c.SetReverseDirection(True)
                _check(c.EndEdit(), "demold control", _ERR["demold"])
                self.s._require_model().ClearSelection2(True)
            do("demold control", demold)
            applied.append(f"demold {kind}")

        do("close setup", lambda: _check(tsm.EndEdit(), "topology setup", _ERR["manager"]))
        return applied

    def _set_options(self, study, preserve_load_faces, max_iterations) -> list:
        notes = []
        try:
            opts = self._ct().hold(self._ct().study(study).TopologyStudyOptions)
            opts.SetPreservedRegionSetting(_PRESERVE["loads_and_fixtures" if preserve_load_faces else "none"])
            if max_iterations:
                opts.SetIterationOption(1)
                opts.SetIterationLimit(int(max_iterations))
        except Exception as exc:  # noqa: BLE001 - the study still runs with Simulation's defaults
            notes.append(f"study options left at Simulation's defaults ({exc})")
        return notes

    # ------------------------------------------------------------------ run, look, check

    def topology_run(self, study: str = "Topology_Techne", element_size_mm: float | None = None) -> dict:
        cw, doc = self._cosmos()
        st = self._study(cw, doc, study)
        if st is None:
            raise SolidWorksError(f"No study '{study}'. sim_list shows the part's studies.")
        size = float(element_size_mm) if element_size_mm else self.default_element_mm()
        start = time.time()
        err = st.CreateMesh(0, size, size * 0.05)
        if err:
            raise SolidWorksError(f"Meshing failed (code {err}); try a smaller element_size_mm.")
        err = st.RunAnalysis()
        if err:
            raise SolidWorksError(f"The topology run failed (code {err}). Check that loads and fixtures sit on "
                                  "faces outside the design space or in preserved regions, and that the "
                                  "limits can be met together.")
        # topology results answer through their vtable only (pywin32 reads 'write-only property')
        res = self._ct().hold(self._ct().study(study).Results)
        names = list(res.GetPlotNames() or ())
        self._ct().release()
        return {"element_size_mm": round(size, 4), "plots": names, "solve_s": round(time.time() - start, 1),
                "next": "topology_views to see the kept material, model it, then topology_verify"}

    def topology_views(self, study: str = "Topology_Techne", folder: str | None = None,
                       views: list | None = None) -> dict:
        """Screenshots of the material-mass result: what the optimizer keeps."""
        cw, doc = self._cosmos()
        if self._study(cw, doc, study) is None:
            raise SolidWorksError(f"No study '{study}'.")
        self._ct().release()
        res = self._ct().hold(self._ct().study(study).Results)
        names = list(res.GetPlotNames() or ())
        plot = next((n for n in names if "mass" in n.lower()), names[0] if names else None)
        if plot is None:
            raise SolidWorksError("The study has no result plots yet: run it first (topology_run).")
        # Simulation's own density cut (the slider in the plot's settings) has no API: ICWPlot.IsoValue
        # stays -1 and AddIsoClippingToPlot answers 'not available for this plot' (live test)
        res.ActivatePlot(plot)
        box = self.s._bounding_box()
        corners = [box["min_mm"], box["max_mm"]] if box else None
        folder = folder or os.path.join(os.path.expanduser("~"), "Documents", "Techne", "topology")
        os.makedirs(folder, exist_ok=True)
        shots = []
        for v in views or ["iso", "front", "top", "right"]:
            v = str(v).lower()
            path = os.path.join(folder, f"{_study_name(study)}_{v}.png")
            self.s.screenshot(path, view=v, zoom_mm=corners)
            entry = {"view": v, "path": path}
            if v in VIEW_AXES:
                entry["screen_right"], entry["screen_up"] = VIEW_AXES[v]
            shots.append(entry)
        return {"ok": True, "study": study, "plot": plot, "bounding_box_mm": box,
                "images": shots,
                "note": "each image is zoomed onto the part's box (bounding_box_mm); screen_right/screen_up give "
                        "the model axes. Read the images, model the kept load paths as sketches, extrudes and "
                        "lofts, then topology_verify on the new part"}

    def topology_sweep(self, fixtures: list, loads: list, mass_reductions: list, study: str = "Topology_Techne",
                       **options) -> dict:
        """One study per mass target (e.g. [30, 50, 70]), each set up and solved."""
        if not mass_reductions:
            raise SolidWorksError("mass_reductions lists the mass targets in percent, e.g. [30, 50, 70].")
        results = []
        for pct in mass_reductions:
            name = _study_name(f"{study}_m{int(round(float(pct)))}")
            try:
                r = self.topology_study(fixtures, loads, mass_reduction_percent=float(pct), study=name,
                                        replace=True, run=True, **options)
                results.append({"mass_reduction_percent": pct, "study": r["study"], "ok": True,
                                "plots": r.get("plots"), "solve_s": r.get("solve_s")})
            except SolidWorksError as exc:
                results.append({"mass_reduction_percent": pct, "study": name, "ok": False, "error": str(exc)})
        return {"ok": any(r["ok"] for r in results), "studies": results,
                "next": "topology_views on each study (same views) to compare how the load paths thin out"}

    def topology_verify(self, study: str = "Topology_Techne", source_part: str | None = None,
                        fixtures: list | None = None, loads: list | None = None, material: str | None = None,
                        element_size_mm: float | None = None) -> dict:
        """The recorded fixtures, loads and limits of a topology study, run as a static study on
        the current part (the one modelled from the result)."""
        rec = self._recall(study, source_part)
        fixtures = fixtures or rec["fixtures"]
        loads = loads or rec["loads"]
        material = material or rec.get("material")
        res = self.sim_static(fixtures, loads, study=_study_name(f"Verify_{rec['study']}"), material=material,
                              element_size_mm=element_size_mm, replace=True)
        mass = self.s.get_mass_properties()["mass_properties"]["mass_kg"]
        design = rec.get("design_space_mass_kg")
        target = mass_target(design, rec.get("mass_reduction_percent"), rec.get("max_mass_kg"))
        lim = rec.get("limits", {})
        checks = []
        if lim.get("max_stress_mpa") is not None:
            v = res["von_mises_max_mpa"]
            checks.append({"check": "von Mises max", "limit_mpa": lim["max_stress_mpa"], "value_mpa": v,
                           "ok": v <= lim["max_stress_mpa"]})
        fos_limit = lim.get("min_factor_of_safety")
        if fos_limit is not None and res.get("factor_of_safety_min") is not None:
            v = res["factor_of_safety_min"]
            checks.append({"check": "factor of safety min", "limit": fos_limit, "value": v, "ok": v >= fos_limit})
        if lim.get("max_displacement_mm") is not None:
            v = res["displacement_max_mm"]
            checks.append({"check": "displacement max", "limit_mm": lim["max_displacement_mm"], "value_mm": v,
                           "ok": v <= lim["max_displacement_mm"]})
        if lim.get("min_frequency_hz") is not None:
            freq = self.sim_frequency(fixtures, modes=1, study=_study_name(f"VerifyF_{rec['study']}"),
                                      material=material, element_size_mm=element_size_mm)["frequencies_hz"]
            v = freq[0] if freq else None
            checks.append({"check": "first natural frequency", "limit_hz": lim["min_frequency_hz"], "value_hz": v,
                           "ok": v is not None and v >= lim["min_frequency_hz"]})
        if target is not None:
            checks.append({"check": "mass", "target_kg": round(target, 6), "value_kg": round(mass, 6),
                           "ok": mass <= target * 1.0001})
        return {"ok": True, "source": {"part": rec.get("part"), "study": rec["study"]}, "static": res,
                "mass_kg": round(mass, 6), "design_space_mass_kg": design,
                "mass_reduction_achieved_percent": round(100.0 * (1.0 - mass / design), 2) if design else None,
                "checks": checks, "passed": all(c["ok"] for c in checks),
                "note": "fixtures and loads were found on the new part from the same face specs; if a spec "
                        "now points at a different face, pass fixtures/loads again"}

    def sim_delete(self, study: str) -> dict:
        cw, doc = self._cosmos()
        if self._study(cw, doc, study) is None:
            raise SolidWorksError(f"No study '{study}'.")
        left = self._delete_studies(cw, doc, [study])
        if left:
            raise SolidWorksError(f"Simulation would not delete '{study}'.")
        return {"ok": True, "deleted": study}
