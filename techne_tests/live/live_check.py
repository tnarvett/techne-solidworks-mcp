"""Live checks of the untested Techne tools, run on a scratch part through run_api_script
while nothing else needs SolidWorks. Each stage keeps under the bridge's 80 s where it can
and writes tests/live_<stage>.json as it goes, so a timed-out call can still be read.

Run (one run_api_script call per stage, in order):
    TECHNE_ROOT = r"<path to your clone>"
    exec(open(TECHNE_ROOT + "/techne_tests/live/live_check.py").read())
    result = stage("plate")          # scratch part: 120 x 40 x 10 plate, 6061-T6
    result = stage("loads")          # directional force + acceleration: signs and reactions
    result = stage("topo_setup")     # stiffness, 50 %, stress limit, half symmetry, thickness
    result = stage("topo_run")       # ~1-2 min: poll tests/live_topo_run.json
    result = stage("topo_views")     # iso, front, top
    result = stage("topo_verify")    # recorded setup re-run as static
    result = stage("controls")       # each other control alone, run=False
    result = stage("split")          # split line + loads on the patch
    result = stage("misc")           # scale_body, sim_frequency, draft timing, import_mesh
    result = stage("cleanup")        # close the scratch parts without saving

The user's own documents are never touched: every stage remembers session._model and
SolidWorks' active document and puts both back.
"""

import importlib.util
import json as _json
import os as _os
import sys as _sys
import time as _time
import traceback as _tb

def _root():
    """The repo folder: TECHNE_ROOT (set before exec) or the environment, else from __file__."""
    if globals().get("TECHNE_ROOT"):
        return globals()["TECHNE_ROOT"]
    if _os.environ.get("TECHNE_ROOT"):
        return _os.environ["TECHNE_ROOT"]
    return _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ".."))


ROOT = _root()
_SRC = _os.path.join(ROOT, "src", "solidworks_mcp")
_TESTS = _os.path.join(ROOT, "techne_tests", "live")
_STATE = _os.path.join(_TESTS, "live_state.json")


def _load_ext():
    """The Techne modules from the source folder (before install) or the installed package."""
    vendor = _os.path.join(_TESTS, "vendor")
    if _os.path.isdir(vendor) and vendor not in _sys.path:
        _sys.path.insert(0, vendor)
    import solidworks_mcp
    if getattr(solidworks_mcp, "__version__", "") == "0.21.0" and _os.path.exists(
            _os.path.join(_os.path.dirname(solidworks_mcp.__file__), "ext_topology.py")):
        import solidworks_mcp.ext_core as core
        import solidworks_mcp.ext_features as feats
        import solidworks_mcp.ext_sketch as sketch
        import solidworks_mcp.ext_mesh as mesh
        import solidworks_mcp.ext_topology as topo
        return core, feats, sketch, mesh, topo
    # not installed yet: load the source fresh every time (sys.modules may hold older copies)
    mods = {}
    for n in ("ext_core", "ext_sim", "ext_features", "ext_sketch", "ext_mesh", "ext_topology"):
        spec = importlib.util.spec_from_file_location("solidworks_mcp." + n, _os.path.join(_SRC, n + ".py"))
        m = importlib.util.module_from_spec(spec)
        _sys.modules["solidworks_mcp." + n] = m
        spec.loader.exec_module(m)
        mods[n] = m
    return mods["ext_core"], mods["ext_features"], mods["ext_sketch"], mods["ext_mesh"], mods["ext_topology"]


def _state(update=None):
    data = {}
    if _os.path.exists(_STATE):
        with open(_STATE, encoding="utf-8") as fh:
            data = _json.load(fh)
    if update:
        data.update(update)
        with open(_STATE, "w", encoding="utf-8") as fh:
            _json.dump(data, fh, indent=1)
    return data


def stage(name):
    out_path = _os.path.join(_TESTS, f"live_{name}.json")
    out = {"stage": name, "started": _time.strftime("%H:%M:%S")}

    def save():
        with open(out_path, "w", encoding="utf-8") as fh:
            _json.dump(out, fh, indent=1, default=str)

    def step(key, fn):
        t = _time.time()
        try:
            out[key] = fn()
        except Exception:  # noqa: BLE001 - record and go on
            out[key] = "ERR " + _tb.format_exc()[-1500:]
        out[key + "_s"] = round(_time.time() - t, 2)
        save()
        return out[key]

    core_m, feats_m, sketch_m, mesh_m, topo_m = _load_ext()
    C = core_m.Core(session)  # noqa: F821 - run_api_script namespace
    F = feats_m.Features(C)
    SK = sketch_m.Sketch(C)
    TOP = topo_m.Topology(C)
    prev = session._model  # noqa: F821
    try:
        prev_active = sw.ActiveDoc.GetTitle()  # noqa: F821
    except Exception:  # noqa: BLE001
        prev_active = None
    st = _state()
    try:
        if name == "plate":
            session.new_part()  # noqa: F821
            step("profile", lambda: session.add_extruded_profile(  # noqa: F821
                [[-60, -20], [60, -20], [60, 20], [-60, 20]], 10, name="Plate"))
            step("material", lambda: session.set_material("6061-T6 (SS)"))  # noqa: F821
            step("bbox", lambda: session._bounding_box())  # noqa: F821
            _state({"plate": session._model.GetTitle()})  # noqa: F821
            out["title"] = session._model.GetTitle()  # noqa: F821
        elif name == "cleanup":
            for key in ("plate", "mesh_part"):
                title = st.get(key)
                if title:
                    step(f"close_{key}", lambda t=title: sw.CloseDoc(t))  # noqa: F821
            _state({"plate": None, "mesh_part": None})
        else:
            title = st.get("plate")
            if not title:
                raise RuntimeError("Run stage('plate') first.")
            from solidworks_mcp import binding as _b
            docs = [_b.wrap(d, session._mod.IModelDoc2) for d in (sw.GetDocuments() or [])]  # noqa: F821
            mine = [d for d in docs if d.GetTitle() == title]
            if not mine:
                raise RuntimeError(f"The scratch part {title} is not open any more; run stage('plate').")
            sw.ActivateDoc3(title, False, 2, 0)  # noqa: F821
            session._model = mine[0]  # noqa: F821
            fixtures = [{"faces": [{"face": "-x"}, {"face": "+x"}]}]
            top_load = [{"faces": [{"face": "+y"}], "force_n": [0, -1000, 0]}]

            if name == "loads":
                from solidworks_mcp.ext_sim import _first as TOP_first  # noqa: N812

                def static_dir():
                    r = TOP.sim_static(fixtures, top_load, study="Live_dir", replace=True)
                    cw, d = TOP._cosmos()
                    stdy = TOP._study(cw, d, "Live_dir")
                    res = _b.wrap(stdy.Results, cw.ICWResults)
                    r["uy_min_max"] = list(TOP_first(res.GetMinMaxDisplacement(1, 1, None, 0)))
                    r["ux_min_max"] = list(TOP_first(res.GetMinMaxDisplacement(0, 1, None, 0)))
                    r["uz_min_max"] = list(TOP_first(res.GetMinMaxDisplacement(2, 1, None, 0)))
                    r["reactions_raw"] = TOP.reaction_raw(cw, stdy, fixtures[0]["faces"])
                    return r
                step("directional_force", static_dir)
                step("hand_calc", lambda: {"fixed_fixed_bending_stress_mpa": 1000 * 120 / 12 * 20 / (10 * 40 ** 3 / 12),
                                           "fixed_fixed_deflection_mm_bending_only":
                                               1000 * 120 ** 3 / (384 * 68.9e3 * (10 * 40 ** 3 / 12)),
                                           "note": "uy_min must be negative (load is -y); reaction Fy +1000"})

                def accel():
                    r = TOP.sim_static(fixtures, [{"acceleration_g": [0, -1, 0]}], study="Live_acc", replace=True)
                    cw, d = TOP._cosmos()
                    stdy = TOP._study(cw, d, "Live_acc")
                    res = _b.wrap(stdy.Results, cw.ICWResults)
                    r["uy_min_max"] = list(TOP_first(res.GetMinMaxDisplacement(1, 1, None, 0)))
                    r["reactions_raw"] = TOP.reaction_raw(cw, stdy, fixtures[0]["faces"])
                    r["expected_weight_n"] = session.get_mass_properties()["mass_properties"]["mass_kg"] * 9.80665  # noqa: F821
                    return r
                step("acceleration", accel)

                def normal():
                    r = TOP.sim_static(fixtures, [{"faces": [{"face": "+y"}], "force_n": 1000}], study="Live_nrm",
                                       replace=True)
                    cw, d = TOP._cosmos()
                    stdy = TOP._study(cw, d, "Live_nrm")
                    res = _b.wrap(stdy.Results, cw.ICWResults)
                    r["uy_min_max"] = list(TOP_first(res.GetMinMaxDisplacement(1, 1, None, 0)))
                    r["note"] = "into the +y face means uy_min negative, like directional_force"
                    return r
                step("normal_force", normal)
                step("delete", lambda: [TOP.sim_delete(n) for n in ("Live_dir", "Live_acc", "Live_nrm")])

            elif name == "topo_setup":
                step("setup", lambda: TOP.topology_study(
                    fixtures, top_load, goal="stiffness", mass_reduction_percent=50, max_stress_mpa=150,
                    min_thickness_mm=3, symmetry={"type": "half", "planes": [{"plane": "right"}]},
                    study="Live_Topo", replace=True, run=False))
                step("studies", TOP.sim_list)

            elif name == "topo_run":
                step("run", lambda: TOP.topology_run("Live_Topo"))

            elif name == "topo_views":
                step("views", lambda: TOP.topology_views("Live_Topo", folder=_os.path.join(_TESTS, "topo_views"),
                                                         views=["iso", "front", "top"]))

            elif name == "topo_verify":
                step("verify", lambda: TOP.topology_verify("Live_Topo"))

            elif name == "controls":
                trials = {
                    "displacement": dict(max_displacement_mm=0.05),
                    "min_mass_disp": dict(goal="min_mass", max_displacement_mm=0.05, mass_reduction_percent=None),
                    "frequency": dict(min_frequency_hz=2000),
                    "fos_only": dict(min_factor_of_safety=3),
                    "preserved_depth": dict(preserved=[{"face": "-y"}], preserved_depth_mm=2),
                    "demold_auto": dict(demold={"type": "two_direction", "edge": {"edge_at": [-60, -20, 5]}}),
                    "demold_pull": dict(demold={"type": "pull", "edge": {"edge_at": [-60, -20, 5]}}),
                    "demold_plane": dict(demold={"type": "two_direction", "edge": {"edge_at": [-60, -20, 5]},
                                                 "plane": {"plane": "front"}}),
                    "max_mass": dict(max_mass_kg=0.05, mass_reduction_percent=None),
                    "max_thickness": dict(max_thickness_mm=15, min_thickness_mm=3),
                }
                for key, kw in trials.items():
                    args = dict(goal="stiffness", mass_reduction_percent=40, study=f"Live_{key}", run=False)
                    args.update(kw)
                    if args.get("mass_reduction_percent") is None:
                        args.pop("mass_reduction_percent")
                    step(key, lambda a=args: TOP.topology_study(fixtures, top_load, **a))
                step("studies", TOP.sim_list)
                for key in trials:
                    step(f"del_{key}", lambda k=key: TOP.sim_delete(f"Live_{k}"))

            elif name == "split":
                # a circle on the Front plane (z = 0) projects onto the plate's -z face
                step("sketch", lambda: SK.sketch_entities([{"circle": {"center": [0, 0], "radius": 8}}],
                                                          plane="front", name="SK_Patch"))
                step("split", lambda: F.split_line("SK_Patch", [{"face": "-z"}], name="Patch"))
                patch = out.get("split", {}).get("faces", []) if isinstance(out.get("split"), dict) else []
                small = sorted(patch, key=lambda f: f["area_mm2"])[:1]
                out["expected_patch_area_mm2"] = round(3.141592653589793 * 8 ** 2, 4)
                if small:
                    step("patch_static", lambda: TOP.sim_static(
                        fixtures, [{"faces": [{"face_at": small[0]["face_at"]}], "force_n": [0, 0, 500]}],
                        study="Live_patch", replace=True))
                    step("del", lambda: TOP.sim_delete("Live_patch"))

            elif name == "misc":
                step("frequency", lambda: TOP.sim_frequency(fixtures, modes=3, study="Live_modal"))
                step("del_modal", lambda: TOP.sim_delete("Live_modal"))
                step("draft", lambda: F.add_draft({"face": "+y"}, [{"face": "-x"}], 1.0, name="LiveDraft"))
                step("scale", lambda: F.scale_body(1.005, name="LiveScale"))
                stl = _os.path.join(_TESTS, "cube10.stl")
                with open(stl, "w", encoding="ascii") as fh:
                    fh.write(_cube_stl(10.0))
                M = mesh_m.Mesh(C)
                step("import_mesh", lambda: M.import_mesh(stl, mode="graphics"))
                try:
                    _state({"mesh_part": session._model.GetTitle()})  # noqa: F821
                except Exception:  # noqa: BLE001
                    pass
            else:
                raise RuntimeError(f"No stage '{name}'.")
    except Exception:  # noqa: BLE001
        out["stage_error"] = _tb.format_exc()[-1500:]
    finally:
        session._model = prev  # noqa: F821
        if prev_active:
            try:
                sw.ActivateDoc3(prev_active, False, 2, 0)  # noqa: F821
            except Exception:  # noqa: BLE001
                pass
        out["finished"] = _time.strftime("%H:%M:%S")
        save()
    return out


def _cube_stl(a):
    """ASCII STL of an a-mm cube, corner at the origin."""
    v = [(0, 0, 0), (a, 0, 0), (a, a, 0), (0, a, 0), (0, 0, a), (a, 0, a), (a, a, a), (0, a, a)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
            (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    lines = ["solid cube"]
    for t in tris:
        p = [v[i] for i in t]
        u = [p[1][k] - p[0][k] for k in range(3)]
        w = [p[2][k] - p[0][k] for k in range(3)]
        n = [u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]]
        ln = sum(c * c for c in n) ** 0.5 or 1.0
        lines.append(" facet normal {:.6f} {:.6f} {:.6f}".format(*(c / ln for c in n)))
        lines.append("  outer loop")
        lines += ["   vertex {:.6f} {:.6f} {:.6f}".format(*q) for q in p]
        lines += ["  endloop", " endfacet"]
    lines.append("endsolid cube")
    return "\n".join(lines) + "\n"
