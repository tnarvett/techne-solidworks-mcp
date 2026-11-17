"""Techne sketch tools: every sketch entity, relations, dimensions, offset/convert,
3D sketches and sketch pictures. Points are millimetres in the sketch's own
[u, v] frame (the frame add_plane / add_sketch report), unless noted."""

import math

import pythoncom
import win32com.client

from . import binding
from .constants import SKETCH_STATUSES, SW_FULLY_CONSTRAINED
from .errors import SolidWorksError
from .ext_core import double_array, enum_value
from .units import m_to_mm, mm_to_m

_RELATIONS = {
    "horizontal": "sgHORIZONTAL2D", "vertical": "sgVERTICAL2D", "coincident": "sgCOINCIDENT",
    "tangent": "sgTANGENT", "parallel": "sgPARALLEL", "perpendicular": "sgPERPENDICULAR",
    "equal": "sgSAMELENGTH", "concentric": "sgCONCENTRIC", "coradial": "sgCORADIAL", "collinear": "sgCOLINEAR",
    "midpoint": "sgATMIDDLE", "symmetric": "sgSYMMETRIC", "fix": "sgFIXED", "pierce": "sgATPIERCE",
    "merge": "sgMERGEPOINTS", "horizontal_points": "sgHORIZPOINTS2D", "vertical_points": "sgVERTPOINTS2D",
    "intersection": "sgATINTERSECT", "equal_curvature": "sgEQUALCURVATURE", "along_x": "sgALONGX3D",
    "along_y": "sgALONGY3D", "along_z": "sgALONGZ3D",
}
_SEG_TYPES = {0: "line", 1: "arc", 2: "ellipse", 3: "spline", 4: "text", 5: "parabola"}


class Sketch:
    def __init__(self, core):
        self.c = core
        self.s = core.s

    @property
    def mod(self):
        return self.s._mod

    # --- open / close ---------------------------------------------------------------

    def _open(self, plane=None, face=None, face_point_mm=None, sketch=None):
        """Open a sketch for drawing; returns (sketch manager, ISketch, existing name or None)."""
        model = self.s._require_model()
        self.s._require_part()
        sk = self.c.skm(model)
        model.ClearSelection2(True)
        if sketch:
            feat = self._sketch_feature(sketch)
            if not feat.Select2(False, 0):
                raise SolidWorksError(f"Could not select {sketch}.")
            model.EditSketch()
            active = binding.wrap(sk.ActiveSketch, self.mod.ISketch)
            if active is None:
                raise SolidWorksError(f"Could not open {sketch} for editing.")
            return sk, active, feat.Name
        if face:
            if face_point_mm is None:
                raise SolidWorksError("face needs face_point_mm, a point on that face.")
            sk, active = self.s._open_sketch_on_face(face, face_point_mm)
            return sk, binding.wrap(active, self.mod.ISketch), None
        ref, label = self.s._named_plane(plane or "front")
        if not ref.Select2(False, 0):
            raise SolidWorksError(f"Could not select the {label}.")
        sk.InsertSketch(True)
        active = binding.wrap(sk.ActiveSketch, self.mod.ISketch)
        if active is None:
            raise SolidWorksError(f"Could not open a sketch on the {label}.")
        return sk, active, None

    def _close(self, sk, three_d=False):
        self.s._model.ClearSelection2(True)
        if three_d:
            sk.Insert3DSketch(True)
        else:
            sk.InsertSketch(True)

    _SKETCH_TYPES = ("ProfileFeature", "3DProfileFeature")

    def _sketch_names(self) -> set:
        return {f.Name for f in self.c.all_features(self.s._model) if f.GetTypeName2() in self._SKETCH_TYPES}

    def _sketch_feature(self, name: str):
        for f in self.c.all_features(self.s._model):
            if f.Name == name and f.GetTypeName2() in self._SKETCH_TYPES:
                return f
        names = sorted(self._sketch_names())
        raise SolidWorksError(f"No sketch '{name}' (there are: {', '.join(names) or 'none'}).")

    def _finish(self, existing, before, name):
        if existing:
            sketch_name = existing
        else:
            new = self._sketch_names() - before
            if len(new) != 1:
                raise SolidWorksError(f"Could not identify the new sketch ({len(new)} new sketches).")
            sketch_name = new.pop()
        if name and name != sketch_name:
            feat = self._sketch_feature(sketch_name)
            feat.Name = name
            sketch_name = feat.Name
        return sketch_name

    def _status(self, sketch_name):
        feat = self._sketch_feature(sketch_name)
        sketch = binding.wrap(feat.GetSpecificFeature2(), self.mod.ISketch)
        status = sketch.GetConstrainedStatus()
        return sketch, {"fully_defined": status == SW_FULLY_CONSTRAINED,
                        "status": SKETCH_STATUSES.get(status, f"status {status}")}

    def segments(self, sketch) -> list:
        out = []
        for raw in sketch.GetSketchSegments() or ():
            seg = binding.wrap(raw, self.mod.ISketchSegment)
            kind = _SEG_TYPES.get(seg.GetType(), str(seg.GetType()))
            entry = {"name": seg.GetName(), "type": kind, "construction": bool(seg.ConstructionGeometry)}
            try:
                if kind == "line":
                    line = binding.wrap(raw, self.mod.ISketchLine)
                    a = binding.wrap(line.GetStartPoint2(), self.mod.ISketchPoint)
                    b = binding.wrap(line.GetEndPoint2(), self.mod.ISketchPoint)
                    entry["uv_mm"] = [[round(m_to_mm(a.X), 4), round(m_to_mm(a.Y), 4)],
                                      [round(m_to_mm(b.X), 4), round(m_to_mm(b.Y), 4)]]
                elif kind == "arc":
                    arc = binding.wrap(raw, self.mod.ISketchArc)
                    c = binding.wrap(arc.GetCenterPoint2(), self.mod.ISketchPoint)
                    entry["center_uv_mm"] = [round(m_to_mm(c.X), 4), round(m_to_mm(c.Y), 4)]
                    entry["radius_mm"] = round(m_to_mm(arc.GetRadius()), 4)
                    entry["circle"] = bool(arc.IsCircle())
            except pythoncom.com_error:
                pass
            entry["length_mm"] = round(m_to_mm(seg.GetLength()), 4)
            out.append(entry)
        return out

    # --- entities ---------------------------------------------------------------------

    @staticmethod
    def _uv(p):
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise SolidWorksError(f"A point is [u, v] in mm (got {p!r}).")
        return mm_to_m(p[0]), mm_to_m(p[1]), mm_to_m(p[2]) if len(p) > 2 else 0.0

    def _draw(self, sk, e) -> int:
        """Draw one entity spec; returns how many entities SolidWorks made."""
        uv = self._uv
        construction = bool(e.get("construction"))
        made = None
        if "line" in e:
            made = sk.CreateLine(*uv(e["line"][0]), *uv(e["line"][1]))
        elif "centerline" in e:
            made = sk.CreateCenterLine(*uv(e["centerline"][0]), *uv(e["centerline"][1]))
        elif "polyline" in e:
            pts = e["polyline"] + ([e["polyline"][0]] if e.get("closed") else [])
            count = 0
            for a, b in zip(pts, pts[1:]):
                seg = sk.CreateLine(*uv(a), *uv(b))
                if seg is None:
                    raise SolidWorksError(f"SolidWorks refused the line {a} -> {b}.")
                if construction:
                    binding.wrap(seg, self.mod.ISketchSegment).ConstructionGeometry = True
                count += 1
            return count
        elif "circle" in e:
            c = e["circle"]
            r = c.get("radius", (c.get("diameter") or 0) / 2.0)
            made = sk.CreateCircleByRadius(*uv(c["center"]), mm_to_m(r))
        elif "arc" in e:
            a = e["arc"]
            if "mid" in a:
                made = sk.Create3PointArc(*uv(a["start"]), *uv(a["end"]), *uv(a["mid"]))
            else:
                made = sk.CreateArc(*uv(a["center"]), *uv(a["start"]), *uv(a["end"]), 1 if a.get("ccw", True) else -1)
        elif "rect" in e:
            r = e["rect"]
            made = sk.CreateCornerRectangle(*uv(r["corner1"]), *uv(r["corner2"]))
        elif "center_rect" in e:
            r = e["center_rect"]
            made = sk.CreateCenterRectangle(*uv(r["center"]), *uv(r["corner"]))
        elif "slot" in e:
            s = e["slot"]
            made = sk.CreateSketchSlot(0, 0, mm_to_m(s["width"]), *uv(s["start"]), *uv(s["end"]), 0, 0, 0, 1, False)
        elif "polygon" in e:
            p = e["polygon"]
            cu, cv, _ = uv(p["center"])
            r, a = mm_to_m(p["radius"]), math.radians(p.get("angle_deg", 0.0))
            made = sk.CreatePolygon(cu, cv, 0, cu + r * math.cos(a), cv + r * math.sin(a), 0, int(p["sides"]),
                                    bool(p.get("inscribed", True)))
        elif "ellipse" in e:
            el = e["ellipse"]
            made = sk.CreateEllipse(*uv(el["center"]), *uv(el["major"]), *uv(el["minor"]))
        elif "spline" in e:
            flat = [c for p in e["spline"] for c in uv(p)]
            made = sk.CreateSpline2(double_array(flat), True)
        elif "point" in e:
            made = sk.CreatePoint(*uv(e["point"]))
        elif "equation" in e:
            q = e["equation"]
            lo, hi = q["range"]
            angle = bool(q.get("angle_range", False))
            if not angle:
                lo, hi = mm_to_m(lo), mm_to_m(hi)
            made = sk.CreateEquationSpline2(q.get("x", ""), q["y"], "", lo, hi, angle,
                                            math.radians(q.get("rotate_deg", 0.0)),
                                            mm_to_m(q.get("offset_mm", [0, 0])[0]),
                                            mm_to_m(q.get("offset_mm", [0, 0])[1]), True, True)
        else:
            raise SolidWorksError(f"Unknown sketch entity {list(e)!r}. Use line, centerline, polyline, circle, arc, "
                                  "rect, center_rect, slot, polygon, ellipse, spline, point or equation.")
        if made is None or made is False:
            raise SolidWorksError(f"SolidWorks refused the entity {e!r}.")
        items = made if isinstance(made, (list, tuple)) else [made]
        if construction:
            for item in items:
                try:
                    binding.wrap(item, self.mod.ISketchSegment).ConstructionGeometry = True
                except pythoncom.com_error:
                    pass
        return len(items)

    def sketch_entities(self, entities: list, plane: str | None = None, face: str | None = None,
                        face_point_mm: list | None = None, sketch: str | None = None, name: str | None = None,
                        fully_define: bool = False) -> dict:
        before = self._sketch_names()
        sk, active, existing = self._open(plane, face, face_point_mm, sketch)
        count = 0
        try:
            sk.AddToDB = True
            for e in entities:
                count += self._draw(sk, e)
            sk.AddToDB = False
            if fully_define:
                sk.FullyDefineSketch(True, True, 1023, True, 1, None, 1, None, 1, 1)
        finally:
            sk.AddToDB = False
            self._close(sk)
        sketch_name = self._finish(existing, before, name)
        sketch_obj, status = self._status(sketch_name)
        return {"ok": True, "sketch": sketch_name, "entities_drawn": count, **status,
                "segments": self.segments(sketch_obj),
                "next": "relations/dimensions: sketch_relation, sketch_dimension, sketch_fully_define; "
                        "then extrude / revolve / sweep"}

    def sketch_info(self, sketch: str) -> dict:
        self.s._reading += 1
        try:
            sketch_obj, status = self._status(sketch)
            points = []
            for raw in sketch_obj.GetSketchPoints2() or ():
                p = binding.wrap(raw, self.mod.ISketchPoint)
                points.append([round(m_to_mm(p.X), 4), round(m_to_mm(p.Y), 4)])
            return {"ok": True, "sketch": sketch, **status, "segments": self.segments(sketch_obj),
                    "points_uv_mm": points[:200]}
        finally:
            self.s._reading -= 1

    def _select_in_sketch(self, active, items) -> None:
        """Select items while a sketch is open: {"segment": "Line1"}, {"sketch_point": [u, v]}
        (nearest point), {"origin": true}, or any model entity spec (edges, faces, planes)."""
        model = self.s._model
        model.ClearSelection2(True)
        sm = self.c.selmgr(model)
        segs = {binding.wrap(r, self.mod.ISketchSegment).GetName(): r for r in active.GetSketchSegments() or ()}
        for i, it in enumerate(items):
            data = binding.wrap(sm.CreateSelectData(), self.mod.ISelectData)
            data.Mark = int(it.get("mark", 0))
            if "segment" in it:
                raw = segs.get(it["segment"])
                if raw is None:
                    raise SolidWorksError(f"No segment '{it['segment']}' in the sketch: {', '.join(segs)}.")
                ok = binding.wrap(raw, self.mod.ISketchSegment).Select4(i > 0, data)
            elif "sketch_point" in it:
                u, v = (mm_to_m(c) for c in it["sketch_point"][:2])
                pts = [binding.wrap(p, self.mod.ISketchPoint) for p in active.GetSketchPoints2() or ()]
                if not pts:
                    raise SolidWorksError("The sketch has no points.")
                best = min(pts, key=lambda p: math.hypot(p.X - u, p.Y - v))
                ok = best.Select4(i > 0, data)
            elif it.get("origin"):
                ok = self.c.ext(model).SelectByID2("Point1@Origin", "EXTSKETCHPOINT", 0, 0, 0, i > 0, data.Mark, None, 0)
            else:
                ent = self.c.resolve(it, model)
                ok = binding.wrap(ent, self.mod.IEntity).Select4(i > 0, data) if not hasattr(ent, "GetTypeName2") \
                    else ent.Select2(i > 0, data.Mark)
            if not ok:
                raise SolidWorksError(f"Could not select {it!r} in the sketch.")

    def sketch_relation(self, sketch: str, relation: str, items: list) -> dict:
        code = _RELATIONS.get(relation.lower())
        if code is None:
            raise SolidWorksError(f"relation is one of {', '.join(_RELATIONS)}.")
        sk, active, _ = self._open(sketch=sketch)
        try:
            self._select_in_sketch(active, items)
            self.s._model.SketchAddConstraints(code)
        finally:
            self._close(sk)
        _, status = self._status(sketch)
        return {"ok": True, "sketch": sketch, "relation": relation, **status}

    def sketch_dimension(self, sketch: str, items: list, value: float, at_uv_mm: list | None = None,
                         angle: bool = False, name: str | None = None) -> dict:
        sk, active, _ = self._open(sketch=sketch)
        model = self.s._model
        try:
            self._select_in_sketch(active, items)
            u, v = at_uv_mm or [0.0, 0.0]
            disp = model.AddDimension2(mm_to_m(u), mm_to_m(v), 0)
            if disp is None:
                raise SolidWorksError("SolidWorks made no dimension for that selection.")
            dim = binding.wrap(binding.wrap(disp, self.mod.IDisplayDimension).GetDimension2(0), self.mod.IDimension)
            dim.SetSystemValue3(math.radians(value) if angle else mm_to_m(value),
                                enum_value("swSetValue_InAllConfigurations"), None)
            if name:
                dim.Name = name
            full = dim.GetNameForSelection()
        finally:
            self._close(sk)
        _, status = self._status(sketch)
        return {"ok": True, "dimension": full, **status,
                "drive_with": f'set_dimension("{full}", ...) or set_equation(\'"{full}" = "GlobalVar"\')'}

    def sketch_fully_define(self, sketch: str) -> dict:
        sk, active, _ = self._open(sketch=sketch)
        try:
            result = sk.FullyDefineSketch(True, True, 1023, True, 1, None, 1, None, 1, 1)
        finally:
            self._close(sk)
        sketch_obj, status = self._status(sketch)
        return {"ok": True, "sketch": sketch, "result": result, **status}

    def sketch_from_edges(self, entities: list, offset_mm: float | None = None, plane: str | None = None,
                          face: str | None = None, face_point_mm: list | None = None, sketch: str | None = None,
                          both_directions: bool = False, construction: bool = False, inner_loops: bool = True,
                          name: str | None = None) -> dict:
        """Convert model edges/faces into the sketch (Convert Entities), or offset them."""
        before = self._sketch_names()
        sk, active, existing = self._open(plane, face, face_point_mm, sketch)
        try:
            model = self.s._model
            self.c.select(entities, False, model)
            had = {binding.wrap(r, self.mod.ISketchSegment).GetName() for r in active.GetSketchSegments() or ()}
            ok = sk.SketchUseEdge3(False, inner_loops)
            if ok and offset_mm is not None:
                # the API offsets sketch entities: offset the converted outline, keep it as construction
                base = [binding.wrap(r, self.mod.ISketchSegment) for r in active.GetSketchSegments() or ()]
                base = [b for b in base if b.GetName() not in had]
                model.ClearSelection2(True)
                for b in base:
                    b.Select4(True, None)
                ok = sk.SketchOffset2(mm_to_m(offset_mm), both_directions, True, 0, construction, True)
                for b in base:
                    b.ConstructionGeometry = True
            if not ok:
                raise SolidWorksError("SolidWorks did not convert/offset the selected entities.")
        finally:
            self._close(sk)
        sketch_name = self._finish(existing, before, name)
        sketch_obj, status = self._status(sketch_name)
        return {"ok": True, "sketch": sketch_name, **status, "segments": self.segments(sketch_obj)}

    def add_3d_sketch(self, segments: list, name: str | None = None) -> dict:
        """Lines, splines, points and 3-point arcs through MODEL [x, y, z] mm points."""
        model = self.s._require_model()
        self.s._require_part()
        before = self._sketch_names()
        sk = self.c.skm(model)
        model.ClearSelection2(True)
        sk.Insert3DSketch(True)
        count = 0
        xyz = lambda p: tuple(mm_to_m(c) for c in p)  # noqa: E731
        try:
            sk.AddToDB = True
            for e in segments:
                if "line" in e:
                    made = sk.CreateLine(*xyz(e["line"][0]), *xyz(e["line"][1]))
                elif "polyline" in e:
                    pts = e["polyline"]
                    made = [sk.CreateLine(*xyz(a), *xyz(b)) for a, b in zip(pts, pts[1:])]
                    made = made if all(m is not None for m in made) else None
                elif "spline" in e:
                    made = sk.CreateSpline2(double_array([c for p in e["spline"] for c in xyz(p)]), True)
                elif "point" in e:
                    made = sk.CreatePoint(*xyz(e["point"]))
                elif "arc" in e:
                    a = e["arc"]
                    made = sk.Create3PointArc(*xyz(a["start"]), *xyz(a["end"]), *xyz(a["mid"]))
                else:
                    raise SolidWorksError(f"3D sketch entity is line, polyline, spline, point or arc (got {list(e)}).")
                if made is None:
                    raise SolidWorksError(f"SolidWorks refused {e!r}.")
                count += len(made) if isinstance(made, list) else 1
        finally:
            sk.AddToDB = False
            self._close(sk, three_d=True)
        sketch_name = self._finish(None, before, name)
        sketch_obj, status = self._status(sketch_name)
        return {"ok": True, "sketch": sketch_name, "entities_drawn": count, **status,
                "segments": self.segments(sketch_obj)}

    def sketch_picture(self, path: str, plane: str = "front", origin_mm: list | None = None,
                       width_mm: float | None = None, height_mm: float | None = None,
                       transparency: float = 0.0, name: str | None = None) -> dict:
        """A scaled reference image in a sketch: trace or measure photos and drawings."""
        before = self._sketch_names()
        sk, active, _ = self._open(plane)
        try:
            pic = binding.wrap(sk.InsertSketchPicture(path), self.mod.ISketchPicture)
            if pic is None:
                raise SolidWorksError(f"SolidWorks could not insert the picture '{path}'.")
            u, v = origin_mm or [0.0, 0.0]
            pic.SetOrigin(mm_to_m(u), mm_to_m(v))
            if width_mm or height_mm:
                pic.SetSize(mm_to_m(width_mm or 0), mm_to_m(height_mm or 0), not (width_mm and height_mm))
            if transparency:
                pic.SetTransparency(1, float(transparency), 0, 0.0)
            size = pic.GetSize()
        finally:
            self._close(sk)
        sketch_name = self._finish(None, before, name)
        return {"ok": True, "sketch": sketch_name,
                "size_mm": [round(m_to_mm(x), 3) for x in size[1:3]] if isinstance(size, tuple) else None}
