"""Techne feature tools: the native features the base server lacked or only
partly covered. Millimetres and degrees; entity arguments are the specs
select_entities takes ({"face": "+z"}, {"edge_at": [x, y, z]}, {"sketch": "Sketch2"}, ...)."""

import math

from . import binding
from .errors import SolidWorksError
from .ext_core import enum_value
from .units import mm_to_m

_END = {"blind": "swEndCondBlind", "through_all": "swEndCondThroughAll", "through_all_both": "swEndCondThroughAllBoth",
        "up_to_next": "swEndCondUpToNext", "up_to_vertex": "swEndCondUpToVertex",
        "up_to_surface": "swEndCondUpToSurface", "offset_from_surface": "swEndCondOffsetFromSurface",
        "mid_plane": "swEndCondMidPlane", "up_to_body": "swEndCondUpToBody"}
_TWIST = {"follow_path": "swTwistControlFollowPath", "keep_normal": "swTwistControlKeepNormalConstant",
          "follow_path_first_guide": "swTwistControlFollowPathFirstGuideCurve",
          "follow_guides": "swTwistControlFollowFirstSecondGuideCurves",
          "constant_twist": "swTwistControlConstantTwistAlongPath"}
_HELIX = {"pitch_revolutions": "swHelixDefinedByPitchAndRevolution",
          "height_revolutions": "swHelixDefinedByHeightAndRevolution",
          "height_pitch": "swHelixDefinedByHeightAndPitch", "spiral": "swHelixDefinedBySpiral"}


class Features:
    def __init__(self, core):
        self.c = core
        self.s = core.s

    @property
    def mod(self):
        return self.s._mod

    def _new_feature(self, before_ids, made, name, of_type=None):
        """The feature a call made: its return if it is a feature, else the newest one in the tree."""
        feat = None
        if made is not None and made is not True:
            try:
                feat = binding.wrap(made, self.mod.IFeature)
                feat.Name  # noqa: B018 - a non-feature return raises here
            except Exception:  # noqa: BLE001
                feat = None
        if feat is None:
            fresh = [f for f in self.c.all_features(self.s._model) if f.GetID() not in before_ids
                     and (of_type is None or f.GetTypeName2() in of_type)]
            fresh = [f for f in fresh if f.GetTypeName2() not in ("ProfileFeature", "3DProfileFeature")] or fresh
            if not fresh:
                return None
            feat = fresh[-1]
        if name:
            feat.Name = name
        return feat

    def _ids(self):
        return {f.GetID() for f in self.c.all_features(self.s._model)}

    def _end(self, key):
        if key not in _END:
            raise SolidWorksError(f"end condition is one of {', '.join(_END)}.")
        return enum_value(_END[key])

    def _select_sketch(self, sketch, mark=0, append=False):
        feat = self.c.feature(sketch, self.s._model)
        if not feat.Select2(append, mark):
            raise SolidWorksError(f"Could not select '{sketch}'.")

    # --- extrude -----------------------------------------------------------------------

    def extrude(self, sketch: str, end: str = "blind", depth_mm: float | None = None, up_to: dict | None = None,
                offset_mm: float = 0.0, reverse: bool = False, end2: str | None = None,
                depth2_mm: float | None = None, up_to2: dict | None = None, cut: bool = False,
                draft_deg: float = 0.0, draft_outward: bool = False, thin_mm: float | None = None,
                thin_both_sides: bool = False, merge: bool = True, flip_side_to_cut: bool = False,
                start_offset_mm: float = 0.0, name: str | None = None) -> dict:
        model = self.s._require_model()
        self.s._require_part()
        t1 = self._end(end)
        t2 = self._end(end2) if end2 else 0
        single = end2 is None
        if end == "through_all_both":  # both ways through everything
            t1 = t2 = enum_value("swEndCondThroughAll")
            single = False
        if end in ("blind", "mid_plane") and depth_mm is None:
            raise SolidWorksError(f"end='{end}' needs depth_mm.")
        if end in ("up_to_vertex", "up_to_surface", "offset_from_surface", "up_to_body") and up_to is None:
            raise SolidWorksError(f"end='{end}' needs up_to, the entity to stop at.")
        # offset_from_surface: offset_mm > 0 stops beyond the surface, < 0 short of it
        d1 = mm_to_m(abs(offset_mm) if end == "offset_from_surface" else (depth_mm or 0.0))
        d2 = mm_to_m(depth2_mm or 0.0)
        model.ClearSelection2(True)
        self._select_sketch(sketch, 0)
        if up_to is not None:
            self.c.select([dict(up_to, mark=1)], True, model)
        if up_to2 is not None:
            self.c.select([dict(up_to2, mark=2)], True, model)
        fm = self.c.fm(model)
        before = self._ids()
        drafted = abs(draft_deg) > 0
        ang = math.radians(abs(draft_deg))
        common = (single, flip_side_to_cut, reverse, t1, t2, d1, d2, drafted, drafted and not single,
                  draft_outward, draft_outward, ang, ang, offset_mm > 0, False, False, False)
        start = (1 if start_offset_mm else 0, mm_to_m(abs(start_offset_mm)), start_offset_mm < 0)
        if thin_mm:
            t = mm_to_m(thin_mm)
            made = fm.FeatureExtrusionThin2(*common, merge, t / 2 if thin_both_sides else t,
                                            t / 2 if thin_both_sides else 0.0, 0.0, False, False, False, 0.0,
                                            False, True, *start)
        elif cut:
            made = fm.FeatureCut4(*common, False, False, True, False, False, False, *start, False)
        else:
            made = fm.FeatureExtrusion3(*common, merge, False, True, *start)
        feat = self._new_feature(before, made, name)
        if feat is None:
            raise SolidWorksError("SolidWorks made no extrude: check the sketch is closed (or open with thin_mm) "
                                  "and that the end condition can be reached.")
        model.ClearSelection2(True)
        return self.c.rebuild_result(model, feature=feat.Name, type=feat.GetTypeName2(),
                                     dimensions=self._dims(feat))

    def _dims(self, feat):
        out = []
        disp = feat.GetFirstDisplayDimension()
        while disp is not None:
            d = binding.wrap(binding.wrap(disp, self.mod.IDisplayDimension).GetDimension2(0), self.mod.IDimension)
            out.append(d.GetNameForSelection())
            disp = feat.GetNextDisplayDimension(disp)
        return out

    # --- revolve -------------------------------------------------------------------------

    def revolve(self, sketch: str, axis: dict | None = None, angle_deg: float = 360.0,
                angle2_deg: float | None = None, mid_plane: bool = False, cut: bool = False,
                thin_mm: float | None = None, reverse: bool = False, merge: bool = True,
                name: str | None = None) -> dict:
        """Revolve a sketch about a centerline in it (found by SolidWorks), or about `axis`:
        {"segment": "Line5"} in that sketch, an axis {"feature": "Axis1"} or an edge spec."""
        model = self.s._require_model()
        self.s._require_part()
        model.ClearSelection2(True)
        self._select_sketch(sketch, 0)
        if axis is not None:
            spec = dict(axis)
            if "segment" in spec:
                spec.setdefault("sketch", sketch)
            self.c.select([dict(spec, mark=16)], True, model)
        single = angle2_deg is None and not mid_plane
        t1 = enum_value("swEndCondMidPlane") if mid_plane else enum_value("swEndCondBlind")
        fm = self.c.fm(model)
        before = self._ids()
        t = mm_to_m(thin_mm or 0.0)
        made = fm.FeatureRevolve2(single, True, bool(thin_mm), cut, reverse, False, t1, 0,
                                  math.radians(angle_deg), math.radians(angle2_deg or 0.0), False, False, 0.0, 0.0,
                                  0, t, t, merge, False, True)
        feat = self._new_feature(before, made, name)
        if feat is None:
            raise SolidWorksError("SolidWorks made no revolve: the sketch needs a centerline (or pass axis) and "
                                  "must not cross the axis.")
        model.ClearSelection2(True)
        return self.c.rebuild_result(model, feature=feat.Name, type=feat.GetTypeName2(), dimensions=self._dims(feat))

    # --- sweep ---------------------------------------------------------------------------

    def sweep(self, path: dict, profile: str | None = None, circular_diameter_mm: float | None = None,
              guides: list | None = None, cut: bool = False, twist: str = "follow_path", twist_deg: float = 0.0,
              merge: bool = True, thin_mm: float | None = None, name: str | None = None) -> dict:
        """Sweep a profile sketch (or a circle of circular_diameter_mm) along a path: a sketch,
        3D sketch, helix ({"feature": "Helix1"}) or edges."""
        model = self.s._require_model()
        self.s._require_part()
        if (profile is None) == (circular_diameter_mm is None):
            raise SolidWorksError("Give either profile (a sketch) or circular_diameter_mm.")
        model.ClearSelection2(True)
        if profile:
            self._select_sketch(profile, 1)
        self.c.select([dict(path, mark=4)], True, model)
        for g in guides or []:
            self.c.select([dict(g, mark=2)], True, model)
        if twist not in _TWIST:
            raise SolidWorksError(f"twist is one of {', '.join(_TWIST)}.")
        tw = enum_value(_TWIST[twist])
        circ = circular_diameter_mm is not None
        dia = mm_to_m(circular_diameter_mm or 0.0)
        t = mm_to_m(thin_mm or 0.0)
        fm = self.c.fm(model)
        before = self._ids()
        if cut:
            made = fm.InsertCutSwept5(False, True, tw, False, False, 0, 0, bool(thin_mm), t, 0.0, 0, 0, False, True,
                                      math.radians(twist_deg), True, False, False, False, circ, dia, 0)
        else:
            made = fm.InsertProtrusionSwept4(False, True, tw, False, False, 0, 0, bool(thin_mm), t, 0.0, 0, 0, merge,
                                             False, True, math.radians(twist_deg), True, circ, dia, 0)
        feat = self._new_feature(before, made, name)
        if feat is None:
            raise SolidWorksError("SolidWorks made no sweep: the profile must be a closed sketch that the path "
                                  "pierces (a pierce relation), or use circular_diameter_mm.")
        model.ClearSelection2(True)
        return self.c.rebuild_result(model, feature=feat.Name, type=feat.GetTypeName2())

    # --- curves and reference geometry ----------------------------------------------------

    def add_helix(self, sketch: str, defined_by: str = "pitch_revolutions", pitch_mm: float = 0.0,
                  revolutions: float = 0.0, height_mm: float = 0.0, clockwise: bool = False,
                  start_angle_deg: float = 0.0, taper_deg: float = 0.0, taper_outward: bool = False,
                  reverse: bool = False, name: str | None = None) -> dict:
        """A helix or spiral on the circle of `sketch` (springs, threads, coils)."""
        model = self.s._require_model()
        self.s._require_part()
        if defined_by not in _HELIX:
            raise SolidWorksError(f"defined_by is one of {', '.join(_HELIX)}.")
        model.ClearSelection2(True)
        self._select_sketch(sketch, 0)
        before = self._ids()
        made = model.InsertHelix(reverse, clockwise, bool(taper_deg), taper_outward, enum_value(_HELIX[defined_by]),
                                 mm_to_m(height_mm), mm_to_m(pitch_mm), float(revolutions),
                                 math.radians(abs(taper_deg)), math.radians(start_angle_deg))
        feat = self._new_feature(before, made, name, of_type=("Helix",))
        if feat is None:
            raise SolidWorksError("SolidWorks made no helix: the sketch must hold exactly one circle.")
        model.ClearSelection2(True)
        return {"ok": True, "feature": feat.Name, "use_as_path": {"feature": feat.Name}}

    def add_draft(self, neutral: dict, faces: list, angle_deg: float, reverse: bool = False,
                  name: str | None = None) -> dict:
        """Neutral-plane draft: tilt `faces` by angle_deg about where they meet `neutral` (a face or plane)."""
        model = self.s._require_model()
        self.s._require_part()
        model.ClearSelection2(True)
        self.c.select([dict(neutral, mark=1)], False, model)
        self.c.select([dict(f, mark=2) for f in faces], True, model)
        before = self._ids()
        made = self.c.fm(model).InsertMultiFaceDraft(math.radians(angle_deg), reverse, False, 0, False, False)
        feat = self._new_feature(before, made, name)
        if feat is None:
            raise SolidWorksError("SolidWorks made no draft: the faces must meet the neutral face/plane.")
        model.ClearSelection2(True)
        return self.c.rebuild_result(model, feature=feat.Name, type=feat.GetTypeName2())

    def add_axis(self, references: list, name: str | None = None) -> dict:
        """A reference axis through: a cylindrical face; two planes; two points/vertices; or an edge."""
        model = self.s._require_model()
        self.c.select(references, False, model)
        before = self._ids()
        model.InsertAxis2(True)
        feat = self._new_feature(before, None, name, of_type=("RefAxis",))
        if feat is None:
            raise SolidWorksError("SolidWorks made no axis from those references.")
        model.ClearSelection2(True)
        return {"ok": True, "feature": feat.Name, "use": {"feature": feat.Name}}

    def add_coordinate_system(self, origin_mm: list, rotate_deg: list | None = None, name: str | None = None) -> dict:
        model = self.s._require_model()
        model.ClearSelection2(True)
        rx, ry, rz = rotate_deg or [0.0, 0.0, 0.0]
        before = self._ids()
        made = self.c.fm(model).CreateCoordinateSystemUsingNumericalValues(
            True, *(mm_to_m(c) for c in origin_mm), bool(rotate_deg), math.radians(rx), math.radians(ry),
            math.radians(rz))
        feat = self._new_feature(before, made, name, of_type=("CoordSys",))
        if feat is None:
            raise SolidWorksError("SolidWorks made no coordinate system.")
        return {"ok": True, "feature": feat.Name}

    def curve_xyz(self, points_mm: list, closed: bool = False, name: str | None = None) -> dict:
        """A 3D curve through XYZ points (Curve Through XYZ Points): tube routes, measured data."""
        model = self.s._require_model()
        self.s._require_part()
        if len(points_mm) < 2:
            raise SolidWorksError("Give at least 2 points.")
        before = self._ids()
        model.InsertCurveFileBegin()
        for p in points_mm + ([points_mm[0]] if closed else []):
            model.InsertCurveFilePoint(*(mm_to_m(c) for c in p))
        made = model.InsertCurveFileEnd()
        feat = self._new_feature(before, None, name)
        if feat is None:
            raise SolidWorksError("SolidWorks made no curve from those points.")
        return {"ok": True, "feature": feat.Name, "type": feat.GetTypeName2(), "use_as_path": {"feature": feat.Name}}

    def scale_body(self, factor: float | list, about: str = "centroid", name: str | None = None) -> dict:
        """Scale the bodies uniformly (factor) or per axis ([sx, sy, sz]) about the centroid,
        origin or a coordinate system by name: casting and print shrink allowances."""
        model = self.s._require_model()
        self.s._require_part()
        model.ClearSelection2(True)
        if about == "centroid":
            kind = enum_value("swScaleAboutCentroid")
        elif about == "origin":
            kind = enum_value("swScaleAboutOrigin")
        else:
            kind = enum_value("swScaleAboutCoordinateSystem")
            self.c.feature(about, model).Select2(False, 0)
        uniform = not isinstance(factor, (list, tuple))
        sx, sy, sz = (factor, factor, factor) if uniform else factor
        before = self._ids()
        made = self.c.fm(model).InsertScale(kind, uniform, float(sx), float(sy), float(sz))
        feat = self._new_feature(before, made, name)
        if feat is None:
            raise SolidWorksError("SolidWorks made no scale feature.")
        return self.c.rebuild_result(model, feature=feat.Name)

    # --- analysis preparation ------------------------------------------------------------

    def split_line(self, sketch: str, faces: list, directional: bool = False, flip: bool = False,
                   name: str | None = None) -> dict:
        """Project a sketch onto faces and split them into separate faces (Split Line): a bolt
        washer's contact patch, a load footprint or a fixture area for Simulation."""
        model = self.s._require_model()
        self.s._require_part()
        model.ClearSelection2(True)
        # marks from the API remarks: the sketch 4, the faces to split 1
        self._select_sketch(sketch, mark=4)
        self.c.select([dict(f, mark=1) for f in faces], True, model)
        before = self._ids()
        model.InsertSplitLineProject(bool(directional), bool(flip))
        feat = self._new_feature(before, None, name)
        model.ClearSelection2(True)
        if feat is None:
            raise SolidWorksError("SolidWorks made no split line: the sketch must project onto the faces "
                                  "(a closed contour or lines that cross them).")
        new_faces = []
        for f in feat.GetFaces() or ():
            face = binding.wrap(f, self.mod.IFace2)
            new_faces.append({"face_at": [round(v * 1000.0, 4) for v in self._interior_point(face)],
                              "area_mm2": round(face.GetArea() * 1e6, 4)})
        return self.c.rebuild_result(model, feature=feat.Name, faces=new_faces,
                                     note="use these face_at specs for loads and fixtures on the split areas")

    def _interior_point(self, face, n: int = 4) -> list:
        """A point on the face as far from its edges as a grid over its box finds, so a face_at spec
        picks this face and not a neighbour sharing an edge (a split patch's centre lies on the
        outer face's hole)."""
        import math
        box = face.GetBox()
        lo, hi = box[:3], box[3:6]
        edges = [binding.wrap(e, self.mod.IEdge) for e in (face.GetEdges() or ())]
        best, best_d, seen = None, -1.0, set()
        for i in range(n + 1):
            for j in range(n + 1):
                for k in range(n + 1):
                    p = [lo[a] + (hi[a] - lo[a]) * t / n for a, t in zip(range(3), (i, j, k))]
                    q = list(face.GetClosestPointOn(*p))[:3]
                    key = tuple(round(v, 7) for v in q)
                    if key in seen:
                        continue
                    seen.add(key)
                    d = min((math.dist(q, list(e.GetClosestPointOn(*q))[:3]) for e in edges), default=0.0)
                    if d > best_d:
                        best, best_d = q, d
        return best

