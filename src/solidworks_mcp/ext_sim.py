"""Techne Simulation tools: drive SOLIDWORKS Simulation (static, frequency, thermal
studies) through its own API. The Simulation add-in must be loaded (Tools > Add-Ins).
Loads in N and Pa (pressure given in MPa); results in MPa, mm, Hz."""

import os

import pythoncom
from win32com.client import dynamic, gencache

from . import binding
from .errors import SolidWorksError

_cw = None


def cw_module(sw):
    """Typed wrappers for the Simulation type library (cosworks.tlb), generated once."""
    global _cw
    if _cw is None:
        path = os.path.join(sw.GetExecutablePath(), "Simulation", "cosworks.tlb")
        if not os.path.exists(path):
            raise SolidWorksError("SOLIDWORKS Simulation is not installed (no Simulation\\cosworks.tlb).")
        attr = pythoncom.LoadTypeLib(path).GetLibAttr()
        _cw = gencache.EnsureModule(str(attr[0]), attr[1], attr[3], attr[4])
    return _cw


def _invoke(obj, name):
    """Late-bound call that also works when pywin32 takes a method for a property (it then
    returns the result on attribute access)."""
    member = getattr(obj, name)
    return member() if callable(member) else member


def _first(ret):
    """Out-parameters come back as a tuple (value, error): take the value, check the error."""
    if isinstance(ret, tuple):
        value, *rest = ret
        err = rest[-1] if rest else 0
        if isinstance(err, int) and err not in (0,):
            raise SolidWorksError(f"Simulation API error code {err}.")
        return value
    return ret


class Simulation:
    def __init__(self, core):
        self.c = core
        self.s = core.s

    def _cosmos(self):
        sw = self.c.sw()
        cw = cw_module(sw)
        addin = sw.GetAddInObject("SldWorks.Simulation")
        if addin is None:  # switched off: load it (as Tools > Add-Ins would), then ask again
            dll = os.path.join(sw.GetExecutablePath(), "Simulation", "cosworks.dll")
            if os.path.exists(dll):
                try:
                    sw.LoadAddIn(dll)
                except Exception:  # noqa: BLE001 - reported below
                    pass
                addin = sw.GetAddInObject("SldWorks.Simulation")
        if addin is None:
            raise SolidWorksError("The SOLIDWORKS Simulation add-in is not loaded and would not load: turn it on in "
                                  "Tools > Add-Ins (it needs a Simulation licence).")
        # the add-in object answers only by name (late binding); what it hands out is typed
        cosmos = binding.wrap(dynamic.Dispatch(addin).CosmosWorks, cw.ICosmosWorks)
        self.s._require_model()  # Simulation works on SolidWorks' active document
        doc = binding.wrap(cosmos.ActiveDoc, cw.ICWModelDoc)
        if doc is None:
            raise SolidWorksError("Simulation sees no active document.")
        return cw, doc

    def default_element_mm(self) -> float:
        """A mesh size from the part's size: its longest side / 25, between 0.5 and 10 mm."""
        try:
            size = max(self.s._bounding_box()["size_mm"])
        except Exception:  # noqa: BLE001
            return 5.0
        return round(min(10.0, max(0.5, size / 25.0)), 3)

    def _study(self, cw, doc, name):
        mgr = binding.wrap(doc.StudyManager, cw.ICWStudyManager)
        for i in range(mgr.StudyCount):
            st = binding.wrap(_first(mgr.GetStudy(i)), cw.ICWStudy)
            if st is not None and st.Name == name:
                return st
        return None

    def _faces(self, specs):
        # Simulation wants a plain array of VARIANTs; a typed VT_DISPATCH array is refused ("memory is locked")
        return [getattr(e, "_oleobj_", e) for e in (self.c.resolve(f) for f in specs)]

    # ------------------------------------------------------------------ loads

    _FIXTURES = {"fixed": 0, "immovable": 1, "roller": 3, "hinge": 4}
    G = 9.80665

    def _front_plane(self):
        """The Front plane as Simulation's direction reference: its directions 1 and 2 and its
        normal are the model's x, y and z (Y-up part; session._DEFAULT_PLANE_AXES)."""
        feat = self.s._named_plane("front")[0]
        return getattr(feat, "_oleobj_", feat)

    @staticmethod
    def _end(code, what):
        code = code() if callable(code) else code
        if isinstance(code, tuple):
            code = next((c for c in reversed(code) if isinstance(c, int)), 0)
        if isinstance(code, int) and code:
            raise SolidWorksError(f"Simulation refused the {what} (error code {code}).")

    def _apply_loads(self, cw, st, fixtures, loads):
        """Fixtures and loads onto a study. Loads (all totals, model axes, N and m/s^2):
        {"faces": [...], "force_n": 500}              normal to the faces, into them
        {"faces": [...], "force_n": [fx, fy, fz]}     a direction in model x, y, z
        {"faces": [...], "pressure_mpa": 2}           normal pressure
        {"acceleration_g": [ax, ay, az]}              body load on the whole part (gravity: [0, -1, 0]);
                                                      or "acceleration_m_s2"."""
        lrm = binding.wrap(st.LoadsAndRestraintsManager, cw.ICWLoadsAndRestraintsManager)
        for fx in fixtures:
            kind = self._FIXTURES.get(fx.get("type", "fixed"))
            if kind is None:
                raise SolidWorksError(f"fixture type is one of {', '.join(self._FIXTURES)}.")
            if _first(lrm.AddRestraint(kind, self._faces(fx["faces"]), None)) is None:
                raise SolidWorksError(f"Could not add the {fx.get('type', 'fixed')} fixture.")
        for ld in loads:
            if "force_n" in ld:
                value = ld["force_n"]
                if isinstance(value, (list, tuple)):
                    vec = [float(v) for v in value]
                    if len(vec) != 3 or not any(vec):
                        raise SolidWorksError("A directional force_n is [fx, fy, fz] in N, not all zero.")
                    ref = self._front_plane()
                    raw = _first(lrm.AddForce2(0, 0, self._faces(ld["faces"]), ref))  # force/moment type
                    if raw is None:
                        raise SolidWorksError("Could not add the force.")
                    force = dynamic.Dispatch(raw)
                    _invoke(force, "ForceBeginEdit")
                    force.SetReferenceGeometry(ref)
                    force.Unit = 0  # SI: newtons
                    force.SetForceComponentValues(*(1 if v else 0 for v in vec), *vec)
                    self._end(_invoke(force, "ForceEndEdit"), "directional force")
                else:
                    raw = _first(lrm.AddForce2(1, 0, self._faces(ld["faces"]), None))  # normal
                    if raw is None:
                        raise SolidWorksError("Could not add the force.")
                    force = dynamic.Dispatch(raw)  # the typed wrapper's edit calls are refused
                    _invoke(force, "ForceBeginEdit")
                    force.Unit = 0
                    force.NormalForceOrTorqueValue = float(value)
                    self._end(_invoke(force, "ForceEndEdit"), "force")
            elif "pressure_mpa" in ld:
                raw = _first(lrm.AddPressure(0, self._faces(ld["faces"]), None))
                if raw is None:
                    raise SolidWorksError("Could not add the pressure.")
                pr = dynamic.Dispatch(raw)
                _invoke(pr, "PressureBeginEdit")
                pr.Unit = 0  # SI: pascals
                pr.Value = float(ld["pressure_mpa"]) * 1e6
                self._end(_invoke(pr, "PressureEndEdit"), "pressure")
            elif "acceleration_g" in ld or "acceleration_m_s2" in ld:
                vec = ld.get("acceleration_m_s2") or [float(v) * self.G for v in ld["acceleration_g"]]
                vec = [float(v) for v in vec]
                if len(vec) != 3 or not any(vec):
                    raise SolidWorksError("An acceleration is [ax, ay, az], not all zero.")
                ref = self._front_plane()
                raw = _first(lrm.AddGravity(ref))
                if raw is None:
                    raise SolidWorksError("Could not add the acceleration (gravity) load.")
                g = dynamic.Dispatch(raw)
                _invoke(g, "GravityBeginEdit")
                g.Unit = 0  # SI: m/s^2
                g.SetGravitationalAcclerationValues(*vec)
                self._end(_invoke(g, "GravityEndEdit"), "acceleration load")
            else:
                raise SolidWorksError("A load gives force_n (number or [fx, fy, fz]), pressure_mpa, or "
                                      "acceleration_g / acceleration_m_s2 [ax, ay, az].")

    def reaction_raw(self, cw, st, specs):
        """Raw GetReactionForcesAndMomentsWithSelections output for the faces (SI): [node values,
        [selection total x8, model total x8], [per face x8 ...], error code]."""
        res = binding.wrap(st.Results, cw.ICWResults)
        ret = res.GetReactionForcesAndMomentsWithSelections(0, None, 0, self._faces(specs))
        return [list(r) if isinstance(r, (tuple, list)) else r for r in (ret if isinstance(ret, tuple) else (ret,))]

    def sim_static(self, fixtures: list, loads: list, study: str = "Static_Techne", material: str | None = None,
                   element_size_mm: float | None = None, replace: bool = True, plot_path: str | None = None) -> dict:
        """Run a linear static study. fixtures: [{"faces": [specs], "type": "fixed"|"immovable"|"roller"|"hinge"}].
        loads: see _apply_loads (normal or directional force, pressure, acceleration).
        Returns peak von Mises (MPa), max displacement (mm) and min factor of safety."""
        if material:
            self.s.set_material(material)
        cw, doc = self._cosmos()
        mgr = binding.wrap(doc.StudyManager, cw.ICWStudyManager)
        existing = self._study(cw, doc, study)
        if existing is not None and replace:
            mgr.DeleteStudy(study)
            existing = None
        st = existing or binding.wrap(_first(mgr.CreateNewStudy3(study, 0, 0)), cw.ICWStudy)
        if st is None:
            raise SolidWorksError(f"Could not create the static study '{study}'.")
        self._apply_loads(cw, st, fixtures, loads)
        size = float(element_size_mm) if element_size_mm else self.default_element_mm()
        tol = size * 0.05
        err = st.CreateMesh(0, size, tol)
        if err:
            raise SolidWorksError(f"Meshing failed (code {err}); try a smaller element_size_mm.")
        err = st.RunAnalysis()
        if err:
            raise SolidWorksError(f"The analysis failed (code {err}); check fixtures and loads.")
        res = binding.wrap(st.Results, cw.ICWResults)
        stress = _first(res.GetMinMaxStress(9, 0, 1, None, 3))  # von Mises, N/mm^2
        disp = _first(res.GetMinMaxDisplacement(3, 1, None, 0))   # URES, mm
        out = {"ok": True, "study": study, "element_size_mm": round(size, 4),
               "von_mises_max_mpa": round(stress[3], 4), "von_mises_min_mpa": round(stress[1], 4),
               "displacement_max_mm": round(disp[3], 6)}
        try:
            fos = _first(res.GetMinMaxFactorOfSafety(True, None, 0, 0))
            out["factor_of_safety_min"] = round(fos[0], 4)
        except Exception:  # noqa: BLE001 - FoS needs a material with a yield strength
            out["factor_of_safety_min"] = None
        reaction = self.fixture_reaction(cw, st, [f for fx in fixtures for f in fx["faces"]])
        if reaction is not None:
            out["reaction_force_n"] = reaction
        if plot_path:
            out["plot"] = self.s.screenshot(plot_path)
        return out

    def fixture_reaction(self, cw, st, specs) -> list | None:
        """Total reaction force [Fx, Fy, Fz] (N, model axes) on the fixture faces: equal and opposite
        to the applied loads when the setup is right (verified 2026-11-14: [500, 0, 300] N applied gave
        [-500, 0, -300], 1 g on 0.1296 kg gave 1.2709 N)."""
        try:
            ret = self.reaction_raw(cw, st, specs)
            total = ret[1]  # [Fx, Fy, Fz, F, Mx, My, Mz, M] for the selection, then the same for the model
            return [round(float(v), 4) for v in total[:3]]
        except Exception:  # noqa: BLE001 - informative only
            return None

    def sim_frequency(self, fixtures: list, modes: int = 5, study: str = "Modal_Techne", material: str | None = None,
                      element_size_mm: float | None = None) -> dict:
        """Natural frequencies (Hz) of the part with the given fixtures."""
        if material:
            self.s.set_material(material)
        cw, doc = self._cosmos()
        mgr = binding.wrap(doc.StudyManager, cw.ICWStudyManager)
        if self._study(cw, doc, study) is not None:
            mgr.DeleteStudy(study)
        st = binding.wrap(_first(mgr.CreateNewStudy3(study, 1, 0)), cw.ICWStudy)
        opts = binding.wrap(st.FrequencyStudyOptions, cw.ICWFrequencyStudyOptions)
        opts.NoOfFrequencies = int(modes)
        self._apply_loads(cw, st, fixtures, [])
        size = float(element_size_mm) if element_size_mm else self.default_element_mm()
        if st.CreateMesh(0, size, size * 0.05):
            raise SolidWorksError("Meshing failed; try a smaller element_size_mm.")
        if st.RunAnalysis():
            raise SolidWorksError("The frequency analysis failed.")
        res = binding.wrap(st.Results, cw.ICWResults)
        flat = list(_first(res.GetResonantFrequencies()) or ())
        # four numbers per mode: mode number, rad/s, Hz, period s (live test: a 120 x 40 x 10 mm
        # 6061 plate fixed at both ends gave mode 1 at 3524 Hz; beam theory 3604 Hz without shear)
        modes_out = [{"mode": int(flat[i]), "hz": round(flat[i + 2], 3)} for i in range(0, len(flat) - 3, 4)]
        return {"ok": True, "study": study, "element_size_mm": round(size, 4),
                "frequencies_hz": [m["hz"] for m in modes_out], "modes": modes_out}

    def sim_list(self) -> dict:
        cw, doc = self._cosmos()
        mgr = binding.wrap(doc.StudyManager, cw.ICWStudyManager)
        studies = []
        for i in range(mgr.StudyCount):
            st = binding.wrap(_first(mgr.GetStudy(i)), cw.ICWStudy)
            studies.append({"name": st.Name, "type": st.AnalysisType})
        return {"ok": True, "studies": studies}
