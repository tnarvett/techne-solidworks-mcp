"""Registers the Techne tools (0.21.0) on the FastMCP server.

Every tool runs on the server's COM thread through the same guarded call as the
base tools (a failed call removes what it added). Long Simulation runs can go to
a background job: the tool returns a job id at once and job_status reports it,
without waiting on SolidWorks.
"""

import asyncio
import sys
import time
import uuid

from .ext_core import Core, log_call
from .ext_eval import Evaluate
from .ext_features import Features
from .ext_mesh import Mesh
from .ext_sketch import Sketch
from .ext_topology import Topology

_JOB_TIMEOUT_S = 3600.0

ENTITY_SPECS = (
    "Entity specs: {'face': '+z' | '+z:inner' | '#5'}, {'face_at': [x,y,z]}, {'edge': 5}, "
    "{'edge_at': [x,y,z]}, {'vertex_at': [x,y,z]}, {'named': 'Bore_Face'}, {'feature': 'Boss1'}, "
    "{'plane': 'front'|'Plane1'}, {'sketch': 'Sketch1'}; mm in model coordinates."
)


def register(mcp, call, session, worker, failure):
    core = Core(session)
    sk_tools = Sketch(core)
    feats = Features(core)
    evaluate = Evaluate(core)
    sim = Topology(core)
    mesh_tools = Mesh(core)
    jobs = {}
    tools = {}

    def tool(fn=None, *, extra=""):
        def deco(f):
            if extra:
                f.__doc__ = (f.__doc__ or "").rstrip() + " " + extra
            tools[f.__name__] = f
            return mcp.tool()(f)
        return deco(fn) if fn is not None else deco

    async def run(name, fn, *args, **kwargs):
        start = time.perf_counter()
        result = await call(fn, *args, **kwargs)
        log_call(name, result, (time.perf_counter() - start) * 1000.0)
        return result

    def start_job(name, fn, *args, **kwargs):
        if worker is None:
            return {"ok": False, "error": "Background jobs need Windows and SolidWorks."}
        job = uuid.uuid4().hex[:8]
        fut = worker.submit(lambda: session.run_guarded(fn, *args, **kwargs))
        jobs[job] = {"tool": name, "future": fut, "started": time.time()}
        return {"ok": True, "job": job, "status": "running",
                "note": "SolidWorks is busy until this finishes (other tools wait). Poll job_status(job)."}

    async def long_call(name, wait, fn, *args, **kwargs):
        if not wait:
            return start_job(name, fn, *args, **kwargs)
        if worker is None:
            return await run(name, fn, *args, **kwargs)
        start = time.perf_counter()
        try:
            fut = worker.submit(lambda: session.run_guarded(fn, *args, **kwargs))
            result = await asyncio.wait_for(asyncio.wrap_future(fut), _JOB_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            result = failure(exc)
        log_call(name, result, (time.perf_counter() - start) * 1000.0)
        return result

    # ------------------------------------------------------------------ core

    @tool
    async def api_lookup(query: str, limit: int = 40) -> dict:
        """Search this SolidWorks' whole API: interfaces, methods (with argument names),
        properties and enum values. 'FeatureExtrusion3', 'IFeatureManager.Insert', 'swEndCond'.
        Use before call_api / create_feature, instead of guessing names."""
        return await run("api_lookup", core.api_lookup, query, limit)

    @tool
    async def get_feature(name: str, interface: str | None = None) -> dict:
        """Read any feature's full definition: every property and getter (API units: m, rad),
        its dimensions, error state, and which properties / Set* methods modify_feature can change."""
        return await run("get_feature", core.get_feature, name, interface)

    @tool
    async def modify_feature(name: str, values: dict | None = None, calls: list | None = None,
                             interface: str | None = None) -> dict:
        """Change any existing feature in place (the native Edit Feature), then rebuild.
        values: {"Property": value}; calls: [{"method": "SetDepth", "args": [true, "25mm"]}].
        Values take units as strings ('12mm', '5deg') or enum names ('swEndCondThroughAll');
        an argument {"entity": spec} passes a face/edge/plane. get_feature lists what can change."""
        return await run("modify_feature", core.modify_feature, name, values, calls, interface)

    @tool
    async def create_feature(feature_type: str, values: dict | None = None, calls: list | None = None,
                             select: list | None = None, name: str | None = None,
                             interface: str | None = None) -> dict:
        """Create any native feature through its definition object (CreateDefinition/CreateFeature):
        the long tail of SolidWorks features (patterns, sheet metal, weldment members, mates...).
        feature_type: a swFeatureNameID_e member ('swFmFillet', 'swFmCurvePattern', ...).
        calls/values as modify_feature (an Initialize call first where the type needs it);
        select: entity specs pre-selected before creation, each with an optional 'mark'."""
        return await run("create_feature", core.create_feature, feature_type, values, calls, select, name, interface)

    @tool
    async def call_api(target: str, member: str, args: list | None = None,
                       set_value: str | float | int | bool | list | dict | None = None,
                       wrap_as: str | None = None, result_as: str | None = None) -> dict:
        """Call any SolidWorks API member without a script. target: sw, doc, ext, fm, skm, selmgr,
        eqmgr, part, asm, drw, cfgmgr, view, modeler, math, 'feature:<name>' or 'handle:<h>'.
        A COM object result comes back as a handle for the next call (result_as / wrap_as name its
        interface). args take units ('5mm') and {"entity": spec}. Find members with api_lookup."""
        return await run("call_api", core.call_api, target, member, args, set_value, wrap_as, result_as)

    @tool(extra=ENTITY_SPECS)
    async def select_entities(items: list, append: bool = False) -> dict:
        """Select faces, edges, vertices, planes, features or sketch segments, each with an optional
        'mark' (the selection mark a SolidWorks command expects). Also {'id': name, 'type': 'AXIS'},
        {'segment': 'Line1', 'sketch': 'Sketch1'}, {'origin': true}."""
        return await run("select_entities", core.select_entities, items, append)

    @tool
    async def name_entity(name: str, entity: dict) -> dict:
        """Give a face, edge or vertex a permanent name ('Bore_Face') that survives rebuilds;
        address it afterwards as {'named': 'Bore_Face'} in any tool."""
        return await run("name_entity", core.name_entity, name, entity)

    @tool
    async def checkpoint(name: str = "auto", to_file: bool = False) -> dict:
        """Remember the part's feature tree and equations now; restore_checkpoint goes back to it.
        to_file also saves a copy to Documents\\Techne\\snapshots."""
        return await run("checkpoint", core.checkpoint, name, to_file)

    @tool
    async def restore_checkpoint(name: str = "auto") -> dict:
        """Undo back to a checkpoint: deletes the features and equations added since."""
        return await run("restore_checkpoint", core.restore_checkpoint, name)

    @tool
    async def rollback(before: str | None = None, after: str | None = None) -> dict:
        """Move the rollback bar before/after a feature to insert new features mid-history;
        call with no arguments to roll forward to the end."""
        return await run("rollback", core.rollback, before, after)

    @tool
    async def rename_feature(name: str, new_name: str) -> dict:
        """Rename a feature, sketch or plane (keep trees readable: 'Bore', 'SK_Flange')."""
        return await run("rename_feature", core.rename_feature, name, new_name)

    @tool
    async def rebuild_report() -> dict:
        """Rebuild and list every feature error and warning by name (with SolidWorks' error
        code name) and every sketch that is not fully defined."""
        return await run("rebuild_report", core.rebuild_report)

    @tool
    async def feature_stats() -> dict:
        """Rebuild time per feature, slowest first (Feature Statistics)."""
        return await run("feature_stats", core.feature_stats)

    @tool
    async def doc_settings(units: str | None = None, standard: str | None = None,
                           decimals: int | None = None) -> dict:
        """Read or set the document's units ('mmgs', 'ips', 'mks', 'cgs'), drafting standard
        ('ansi', 'iso', 'din', 'jis') and linear decimal places."""
        return await run("doc_settings", core.doc_settings, units, standard, decimals)

    @tool
    async def custom_properties(values: dict | None = None, delete: list | None = None,
                                configuration: str = "") -> dict:
        """Set, delete and list custom properties (file-level, or of a configuration):
        PartNo, Material, Source, EvidenceTier... Returns every property with its value."""
        return await run("custom_properties", core.custom_properties, values, delete, configuration)

    @tool
    async def configurations(action: str = "list", name: str | None = None, parent: str | None = None,
                             description: str = "") -> dict:
        """List, add, activate or delete configurations (variants of one part)."""
        return await run("configurations", core.configurations, action, name, parent, description)

    @tool
    async def watch_changes() -> dict:
        """What changed in the current document since the last call: features added, removed,
        renamed or (un)suppressed, equations changed, plus the Techne add-in's journal of edits
        made by hand in SolidWorks. Call it before continuing work the user may have touched."""
        return await run("watch_changes", core.watch_changes)

    @tool
    async def telemetry_summary(last: int = 1000) -> dict:
        """Calls, failures and average time per tool from the local telemetry log: where the
        tooling is slow or failing, to improve it."""
        return core.telemetry_summary(last)

    @tool
    async def run_recipe(steps: list, rollback_on_error: bool = True) -> dict:
        """Run many tool calls in one go: steps = [{"tool": "add_box", "args": {...}}, ...].
        Stops at the first failure; with rollback_on_error the part goes back to how it was
        before the recipe. Returns each step's key results and the final mass properties."""
        server = sys.modules.get("solidworks_mcp.server")
        taken = await call(core.checkpoint, "_recipe") if worker is not None else {"ok": False}
        out = []
        for i, step in enumerate(steps):
            name = step.get("tool")
            fn = tools.get(name) or (getattr(server, name, None) if server else None)
            if fn is None or name == "run_recipe":
                out.append({"step": i, "tool": name, "ok": False, "error": "unknown tool"})
                break
            result = await fn(**(step.get("args") or {}))
            brief = {k: v for k, v in result.items() if k in ("ok", "error", "feature", "sketch", "rebuild_ok",
                                                              "feature_errors", "dimensions", "fully_defined")}
            out.append({"step": i, "tool": name, **brief})
            if not result.get("ok", True):
                restored = None
                if rollback_on_error and taken.get("ok"):
                    restored = await call(core.restore_checkpoint, "_recipe")
                return {"ok": False, "failed_step": i, "steps": out,
                        "rolled_back": bool(restored and restored.get("ok"))}
        final = await call(session.get_mass_properties)
        return {"ok": True, "steps": out, "mass_properties": final.get("mass_properties")}

    @tool
    async def job_status(job: str | None = None) -> dict:
        """State of background jobs (long Simulation and topology runs): running with elapsed
        seconds, or the finished result. Without a job id, lists all."""
        def entry(key, j):
            fut = j["future"]
            e = {"job": key, "tool": j["tool"], "elapsed_s": round(time.time() - j["started"], 1)}
            if not fut.done():
                e["status"] = "running"
            else:
                exc = fut.exception()
                e["status"] = "failed" if exc else "done"
                e["result"] = failure(exc) if exc else fut.result()
            return e
        if job is None:
            return {"ok": True, "jobs": [entry(k, j) for k, j in jobs.items()]}
        if job not in jobs:
            return {"ok": False, "error": f"No job '{job}'."}
        return {"ok": True, **entry(job, jobs[job])}

    # ------------------------------------------------------------------ sketch

    @tool
    async def sketch_entities(entities: list, plane: str | None = None, face: str | None = None,
                              face_point_mm: list | None = None, sketch: str | None = None,
                              name: str | None = None, fully_define: bool = False) -> dict:
        """Draw any sketch entities, on a plane (front/top/right/name), a face (face + face_point_mm)
        or into an existing sketch. Points are [u, v] mm in the sketch's frame. Entities:
        {"line": [p1, p2]}, {"centerline": [p1, p2]}, {"polyline": [p...], "closed": true},
        {"circle": {"center": p, "radius"|"diameter": r}}, {"arc": {"center", "start", "end", "ccw"}} or
        {"arc": {"start", "mid", "end"}}, {"rect": {"corner1", "corner2"}}, {"center_rect": {"center", "corner"}},
        {"slot": {"start", "end", "width"}}, {"polygon": {"center", "radius", "sides", "inscribed", "angle_deg"}},
        {"ellipse": {"center", "major", "minor"}}, {"spline": [p...]}, {"point": p},
        {"equation": {"y": "0.005*sin(x/0.005)", "x": "(parametric)", "range": [a, b], "angle_range": false}}
        (equations in metres; range in mm, or radians with angle_range). Any entity may carry
        "construction": true. Returns segment names for sketch_relation / sketch_dimension."""
        return await run("sketch_entities", sk_tools.sketch_entities, entities, plane, face, face_point_mm,
                         sketch, name, fully_define)

    @tool
    async def sketch_info(sketch: str) -> dict:
        """A sketch's segments (names, types, [u, v] coordinates, construction), points and status."""
        return await run("sketch_info", sk_tools.sketch_info, sketch)

    @tool
    async def sketch_relation(sketch: str, relation: str, items: list) -> dict:
        """Add a relation: horizontal, vertical, coincident, tangent, parallel, perpendicular, equal,
        concentric, coradial, collinear, midpoint, symmetric, fix, pierce, merge, intersection...
        items: {"segment": "Line1"}, {"sketch_point": [u, v]} (nearest point), {"origin": true},
        or a model entity spec (an edge for pierce/coincident)."""
        return await run("sketch_relation", sk_tools.sketch_relation, sketch, relation, items)

    @tool
    async def sketch_dimension(sketch: str, items: list, value: float, at_uv_mm: list | None = None,
                               angle: bool = False, name: str | None = None) -> dict:
        """Add a driving dimension to 1-2 items (as sketch_relation) and set it: mm, or degrees with
        angle=true. name renames it; returns the full name for set_dimension / equations."""
        return await run("sketch_dimension", sk_tools.sketch_dimension, sketch, items, value, at_uv_mm, angle, name)

    @tool
    async def sketch_fully_define(sketch: str) -> dict:
        """SolidWorks' Fully Define Sketch: adds the relations and dimensions still missing,
        from the origin."""
        return await run("sketch_fully_define", sk_tools.sketch_fully_define, sketch)

    @tool
    async def sketch_from_edges(entities: list, offset_mm: float | None = None, plane: str | None = None,
                                face: str | None = None, face_point_mm: list | None = None,
                                sketch: str | None = None, both_directions: bool = False,
                                construction: bool = False, inner_loops: bool = True,
                                name: str | None = None) -> dict:
        """Convert Entities (model edges or a face's outline into a sketch), or Offset Entities with
        offset_mm (the converted outline stays as construction). Gaskets, lips, offsets of a flange."""
        return await run("sketch_from_edges", sk_tools.sketch_from_edges, entities, offset_mm, plane, face,
                         face_point_mm, sketch, both_directions, construction, inner_loops, name)

    @tool
    async def add_3d_sketch(segments: list, name: str | None = None) -> dict:
        """A 3D sketch through MODEL [x, y, z] mm points: {"line": [p1, p2]}, {"polyline": [p...]},
        {"spline": [p...]}, {"point": p}, {"arc": {"start", "mid", "end"}}. Sweep paths, runners,
        headers, harness and tube routes."""
        return await run("add_3d_sketch", sk_tools.add_3d_sketch, segments, name)

    @tool
    async def sketch_picture(path: str, plane: str = "front", origin_mm: list | None = None,
                             width_mm: float | None = None, height_mm: float | None = None,
                             transparency: float = 0.0, name: str | None = None) -> dict:
        """Put a reference image (photo, drawing, catalog page) in a sketch at a known scale, to
        trace or measure from (tag such dimensions E4)."""
        return await run("sketch_picture", sk_tools.sketch_picture, path, plane, origin_mm, width_mm, height_mm,
                         transparency, name)

    # ------------------------------------------------------------------ features

    @tool
    async def extrude(sketch: str, end: str = "blind", depth_mm: float | None = None, up_to: dict | None = None,
                      offset_mm: float = 0.0, reverse: bool = False, end2: str | None = None,
                      depth2_mm: float | None = None, up_to2: dict | None = None, cut: bool = False,
                      draft_deg: float = 0.0, draft_outward: bool = False, thin_mm: float | None = None,
                      thin_both_sides: bool = False, merge: bool = True, flip_side_to_cut: bool = False,
                      start_offset_mm: float = 0.0, name: str | None = None) -> dict:
        """Boss or cut extrude with every end condition: blind, through_all, through_all_both,
        up_to_next, up_to_vertex, up_to_surface, offset_from_surface (offset_mm > 0 beyond the surface),
        mid_plane, up_to_body; a second direction (end2/depth2_mm/up_to2); draft; thin walls from
        open or closed sketches; a start offset."""
        return await run("extrude", feats.extrude, sketch, end, depth_mm, up_to, offset_mm, reverse, end2,
                         depth2_mm, up_to2, cut, draft_deg, draft_outward, thin_mm, thin_both_sides, merge,
                         flip_side_to_cut, start_offset_mm, name)

    @tool
    async def revolve(sketch: str, axis: dict | None = None, angle_deg: float = 360.0,
                      angle2_deg: float | None = None, mid_plane: bool = False, cut: bool = False,
                      thin_mm: float | None = None, reverse: bool = False, merge: bool = True,
                      name: str | None = None) -> dict:
        """Revolved boss or cut (O-ring, circlip and ring grooves; valve seats; pulleys). The sketch's
        centerline is the axis, or axis = {"segment": "Line5"} / {"feature": "Axis1"} / an edge."""
        return await run("revolve", feats.revolve, sketch, axis, angle_deg, angle2_deg, mid_plane, cut,
                         thin_mm, reverse, merge, name)

    @tool
    async def sweep(path: dict, profile: str | None = None, circular_diameter_mm: float | None = None,
                    guides: list | None = None, cut: bool = False, twist: str = "follow_path",
                    twist_deg: float = 0.0, merge: bool = True, thin_mm: float | None = None,
                    name: str | None = None) -> dict:
        """Swept boss or cut along a 2D/3D sketch, a helix, an XYZ curve or edges ({"sketch": "P3"},
        {"feature": "Helix1"}). Profile = a closed sketch pierced by the path, or a circle of
        circular_diameter_mm (springs, tubes, wires). twist: follow_path, keep_normal, follow_guides,
        constant_twist (twist_deg)."""
        return await run("sweep", feats.sweep, path, profile, circular_diameter_mm, guides, cut, twist,
                         twist_deg, merge, thin_mm, name)

    @tool
    async def add_helix(sketch: str, defined_by: str = "pitch_revolutions", pitch_mm: float = 0.0,
                        revolutions: float = 0.0, height_mm: float = 0.0, clockwise: bool = False,
                        start_angle_deg: float = 0.0, taper_deg: float = 0.0, taper_outward: bool = False,
                        reverse: bool = False, name: str | None = None) -> dict:
        """Helix/spiral on a sketch's circle (defined_by: pitch_revolutions, height_revolutions,
        height_pitch, spiral): springs, threads, coils; sweep along it."""
        return await run("add_helix", feats.add_helix, sketch, defined_by, pitch_mm, revolutions, height_mm,
                         clockwise, start_angle_deg, taper_deg, taper_outward, reverse, name)

    @tool
    async def add_draft(neutral: dict, faces: list, angle_deg: float, reverse: bool = False,
                        name: str | None = None) -> dict:
        """Neutral-plane draft: tilt faces by angle_deg about the neutral face/plane (castings,
        moulded and printed-mould parts)."""
        return await run("add_draft", feats.add_draft, neutral, faces, angle_deg, reverse, name)

    @tool
    async def add_axis(references: list, name: str | None = None) -> dict:
        """Reference axis: through a cylindrical face, two planes, two points or an edge."""
        return await run("add_axis", feats.add_axis, references, name)

    @tool
    async def add_coordinate_system(origin_mm: list, rotate_deg: list | None = None,
                                    name: str | None = None) -> dict:
        """Coordinate system at origin_mm, rotated [rx, ry, rz] degrees: for mass properties about
        a crank or bore axis, exports and drawings."""
        return await run("add_coordinate_system", feats.add_coordinate_system, origin_mm, rotate_deg, name)

    @tool
    async def curve_xyz(points_mm: list, closed: bool = False, name: str | None = None) -> dict:
        """Curve Through XYZ Points: a 3D curve from points (tube routes, measured data, cam lift);
        sweep along it."""
        return await run("curve_xyz", feats.curve_xyz, points_mm, closed, name)

    @tool
    async def scale_body(factor: float | list, about: str = "centroid", name: str | None = None) -> dict:
        """Scale the bodies uniformly or per axis about the centroid, origin or a coordinate system:
        casting and 3D-print shrink allowances."""
        return await run("scale_body", feats.scale_body, factor, about, name)

    @tool(extra=ENTITY_SPECS)
    async def split_line(sketch: str, faces: list, directional: bool = False, flip: bool = False,
                         name: str | None = None) -> dict:
        """Split Line (Analysis Preparation): project a sketch onto faces so its outline becomes
        separate faces, e.g. a washer-sized circle around a bolt hole as the patch a load or
        fixture acts on, instead of the whole face. Returns the new faces as face_at specs."""
        return await run("split_line", feats.split_line, sketch, faces, directional, flip, name)

    # ------------------------------------------------------------------ evaluate

    @tool(extra=ENTITY_SPECS)
    async def measure(entities: list) -> dict:
        """The Measure tool on 1+ entities: distance, normal/center distance, angle, diameter,
        radius, lengths, areas, deltas, parallel/perpendicular."""
        return await run("measure", evaluate.measure, entities)

    @tool
    async def section_properties(faces: list) -> dict:
        """Section properties of planar faces: area, centroid, Ixx/Iyy/Izz, products, polar J,
        principal moments (mm, mm^4). Tube and beam stiffness, port flow area."""
        return await run("section_properties", evaluate.section_properties, faces)

    @tool
    async def check_geometry() -> dict:
        """Tools > Check on every body: faults count per body (imports, mesh conversions, lofts)."""
        return await run("check_geometry", evaluate.check_geometry)

    @tool
    async def mass_properties_about(coordinate_system: str | None = None) -> dict:
        """Mass, center of mass, inertia tensor and principal moments (kg, mm, kg*mm^2), about the
        center of mass and a named coordinate system."""
        return await run("mass_properties_about", evaluate.mass_properties_about, coordinate_system)

    # ------------------------------------------------------------------ simulation

    @tool
    async def sim_static(fixtures: list, loads: list, study: str = "Static_Techne", material: str | None = None,
                         element_size_mm: float | None = None, plot_path: str | None = None,
                         wait: bool = True) -> dict:
        """Linear static FEA (SOLIDWORKS Simulation). fixtures: [{"faces": [specs], "type": "fixed"|
        "immovable"|"roller"|"hinge"}]. loads (totals, model axes): {"faces": [specs], "force_n": 500}
        normal into the faces; {"faces": [...], "force_n": [fx, fy, fz]} in a direction (N);
        {"faces": [...], "pressure_mpa": 2}; {"acceleration_g": [ax, ay, az]} body load on the whole
        part (gravity [0, -1, 0], a 3 g bump [0, -3, 0]). Returns peak von Mises MPa, max
        displacement mm, min factor of safety and reaction_force_n (total on the fixtures; it should
        be equal and opposite to the loads, a check that they went where intended). wait=false runs
        it as a background job (job_status)."""
        return await long_call("sim_static", wait, sim.sim_static, fixtures, loads, study, material,
                               element_size_mm, True, plot_path)

    @tool
    async def sim_frequency(fixtures: list, modes: int = 5, study: str = "Modal_Techne",
                            material: str | None = None, element_size_mm: float | None = None,
                            wait: bool = True) -> dict:
        """Natural frequencies (Hz) with the given fixtures (brackets, mounts, intake runners)."""
        return await long_call("sim_frequency", wait, sim.sim_frequency, fixtures, modes, study, material,
                               element_size_mm)

    @tool
    async def sim_list() -> dict:
        """The Simulation studies of the active part."""
        return await run("sim_list", sim.sim_list)

    @tool
    async def sim_delete(study: str) -> dict:
        """Delete a Simulation study of the active part by name (sim_list shows them)."""
        return await run("sim_delete", sim.sim_delete, study)

    @tool(extra=ENTITY_SPECS)
    async def topology_study(fixtures: list, loads: list, goal: str = "stiffness",
                             mass_reduction_percent: float | None = None, max_mass_kg: float | None = None,
                             max_stress_mpa: float | None = None, min_factor_of_safety: float | None = None,
                             max_displacement_mm: float | None = None, displacement_component: str = "ures",
                             min_frequency_hz: float | None = None, preserved: list | None = None,
                             preserved_depth_mm: float = 0.0, preserve_load_faces: bool = True,
                             symmetry: dict | None = None, min_thickness_mm: float | None = None,
                             max_thickness_mm: float | None = None, demold: dict | None = None,
                             study: str = "Topology_Techne", material: str | None = None,
                             element_size_mm: float | None = None, max_iterations: int | None = None,
                             replace: bool = False, run: bool = True, wait: bool = False) -> dict:
        """Topology optimization (SOLIDWORKS Simulation): the current part is the design space, so
        model it as the full envelope the part may fill (bolt bosses and interface faces included).
        goal: "stiffness" (best stiffness-to-weight; needs a mass target, 50 % if none given),
        "min_displacement" (needs a mass target), "min_mass" (needs a limit: displacement, stress/FoS
        or frequency; no mass target). Limits: mass_reduction_percent or max_mass_kg; max_stress_mpa
        (von Mises, set as FoS = yield / limit, so the material needs a yield strength; SolidWorks'
        '6061 Alloy' is annealed, 55 MPa, use '6061-T6 (SS)' for T6) and/or min_factor_of_safety;
        max_displacement_mm on displacement_component ures|ux|uy|uz|ux_abs|uy_abs|uz_abs;
        min_frequency_hz (mode 1). Manufacturing: preserved faces (+preserved_depth_mm) kept solid;
        preserve_load_faces keeps faces with fixtures/loads (default on); symmetry {"type":
        "half"|"quarter"|"eighth", "planes": [1|2|3 specs]}; min/max member thickness (mm); demold
        {"type": "two_direction"|"pull"|"stamping", "edge": spec of a straight edge along the pull
        direction (required), "plane": central plane (two_direction; automatic if left out),
        "reverse": bool}. fixtures/loads as
        sim_static. An existing study name gets a _2 suffix unless replace=true. Runs as a
        background job by default (poll job_status, about 1-2 min for a small part); then
        topology_views, model the result, topology_verify."""
        return await long_call("topology_study", wait, sim.topology_study, fixtures, loads, goal,
                               mass_reduction_percent, max_mass_kg, max_stress_mpa, min_factor_of_safety,
                               max_displacement_mm, min_frequency_hz, preserved, preserved_depth_mm, symmetry,
                               min_thickness_mm, max_thickness_mm, demold, study, material, element_size_mm,
                               max_iterations, run, displacement_component, preserve_load_faces, replace)

    @tool
    async def topology_sweep(fixtures: list, loads: list, mass_reductions: list, goal: str = "stiffness",
                             max_stress_mpa: float | None = None, min_factor_of_safety: float | None = None,
                             max_displacement_mm: float | None = None, preserved: list | None = None,
                             symmetry: dict | None = None, min_thickness_mm: float | None = None,
                             study: str = "Topology_Techne", material: str | None = None,
                             element_size_mm: float | None = None, wait: bool = False) -> dict:
        """Several topology studies in one background job, one per mass target (e.g. [30, 50, 70]),
        named <study>_m30 etc., the rest as topology_study. Compare them with topology_views."""
        return await long_call("topology_sweep", wait, sim.topology_sweep, fixtures, loads, mass_reductions,
                               study, goal=goal, max_stress_mpa=max_stress_mpa,
                               min_factor_of_safety=min_factor_of_safety, max_displacement_mm=max_displacement_mm,
                               preserved=preserved, symmetry=symmetry, min_thickness_mm=min_thickness_mm,
                               material=material, element_size_mm=element_size_mm)

    @tool
    async def topology_run(study: str = "Topology_Techne", element_size_mm: float | None = None,
                           wait: bool = False) -> dict:
        """Mesh and solve a topology study set up with run=false (background job by default)."""
        return await long_call("topology_run", wait, sim.topology_run, study, element_size_mm)

    @tool
    async def topology_views(study: str = "Topology_Techne", folder: str | None = None,
                             views: list | None = None) -> dict:
        """PNG views (iso, front, top, right by default; also back, bottom, left) of the material the
        optimizer keeps (Material Mass plot; its caption gives the kept mass), each zoomed onto the
        part's bounding box (returned, mm) with the model axes that point right and up on screen.
        Read the images, then model the load paths as a clean parametric part."""
        return await run("topology_views", sim.topology_views, study, folder, views)

    @tool
    async def topology_verify(study: str = "Topology_Techne", source_part: str | None = None,
                              fixtures: list | None = None, loads: list | None = None,
                              material: str | None = None, element_size_mm: float | None = None,
                              wait: bool = True) -> dict:
        """Check the part modelled from a topology result: the study's recorded fixtures, loads,
        material and limits (stress, FoS, displacement, frequency, mass target) run on the current
        part as a static study. Returns each check with its value and pass/fail. source_part picks
        the part the study was on when several have that study name; fixtures/loads override the
        recorded ones when the new geometry needs other face specs."""
        return await long_call("topology_verify", wait, sim.topology_verify, study, source_part, fixtures,
                               loads, material, element_size_mm)

    # ------------------------------------------------------------------ mesh

    @tool
    async def import_mesh(path: str, mode: str = "graphics", units: str = "mm", mesh_brep: bool = True) -> dict:
        """Open an STL/OBJ/3MF/PLY in SolidWorks as a graphics body (to trace and compare) or as a
        mesh BREP solid/surface (to cut, fillet, Boolean). It becomes the current part."""
        return await run("import_mesh", mesh_tools.import_mesh, path, mode, units, mesh_brep)

    return tools
