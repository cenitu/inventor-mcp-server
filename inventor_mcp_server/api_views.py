"""Pictures of the model."""
if __package__:
    from .api_common import *  # noqa: F401,F403
    from .flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401
else:  # server.py run as a script: the modules sit on sys.path
    from api_common import *  # noqa: F401,F403
    from flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401


class ViewsMixin:
    # ---------- Pictures of the model ----------

    VIEWS = {"iso": 10759, "iso_left": 10760, "iso_bottom": 10761, "front": 10764, "back": 10756,
             "top": 10754, "bottom": 10757, "left": 10758, "right": 10755, "current": 10765}

    _CLUTTER = ("Sketches", "Sketches3D", "UserWorkPlanes", "UserWorkAxes", "UserWorkPoints",
                "OriginWorkPlanes", "OriginWorkAxes", "OriginWorkPoints")

    # Z-up views: (direction from the model to the eye, up vector)
    _ZUP = {"iso": ((1, -1, 1), (0, 0, 1)), "iso_left": ((-1, -1, 1), (0, 0, 1)),
            "iso_bottom": ((1, -1, -1), (0, 0, 1)), "front": ((0, -1, 0), (0, 0, 1)),
            "back": ((0, 1, 0), (0, 0, 1)), "top": ((0, 0, 1), (0, 1, 0)),
            "bottom": ((0, 0, -1), (0, -1, 0)), "left": ((-1, 0, 0), (0, 0, 1)),
            "right": ((1, 0, 0), (0, 0, 1))}

    def _z_up_view(self, cam, name):
        """Point the camera for a model built with Z up (Inventor's named views assume Y up)."""
        rb = self.active_document().ComponentDefinition.RangeBox
        lo, hi = rb.MinPoint, rb.MaxPoint
        center = [(lo.X + hi.X) / 2, (lo.Y + hi.Y) / 2, (lo.Z + hi.Z) / 2]
        span = max(hi.X - lo.X, hi.Y - lo.Y, hi.Z - lo.Z, 1.0)
        (dx, dy, dz), up = self._ZUP[name]
        tg = self._tg()
        cam.Target = tg.CreatePoint(*center)
        cam.Eye = tg.CreatePoint(center[0] + dx * span * 3, center[1] + dy * span * 3, center[2] + dz * span * 3)
        cam.UpVector = tg.CreateUnitVector(*up)

    _UP = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}

    def _custom_view(self, cam, look_from, target, up, box):
        """Point the camera from look_from (mm) at target (mm; default: the middle of box, or
        of the model). With a target the picture is framed on it, sized to box / the model;
        without one the whole model is fitted along that direction."""
        if box is None:
            box = self.active_document().ComponentDefinition.RangeBox
        lo, hi = box.MinPoint, box.MaxPoint
        eye = [float(c) / 10 for c in look_from]
        aim = ([float(c) / 10 for c in target] if target is not None
               else [(lo.X + hi.X) / 2, (lo.Y + hi.Y) / 2, (lo.Z + hi.Z) / 2])
        d = [a - e for a, e in zip(aim, eye)]
        length = math.sqrt(sum(c * c for c in d))
        if length < 1e-6:
            raise ValueError("look_from and target are the same point")
        upv = up if not isinstance(up, str) else self._UP.get(up.lower(), (0, 1, 0))
        n = math.sqrt(sum(float(c) ** 2 for c in upv)) or 1.0
        upv = [float(c) / n for c in upv]
        if abs(sum(a * b for a, b in zip(d, upv))) / length > 0.999:  # looking along `up`
            upv = next(v for v in ((0, 1, 0), (0, 0, 1), (1, 0, 0))
                       if abs(sum(a * b for a, b in zip(d, v))) / length < 0.999)
        tg = self._tg()
        cam.Eye, cam.Target = tg.CreatePoint(*eye), tg.CreatePoint(*aim)
        cam.UpVector = tg.CreateUnitVector(*upv)
        if target is None:
            cam.Fit()
        else:
            span = max(hi.X - lo.X, hi.Y - lo.Y, hi.Z - lo.Z, 0.1) * 1.3
            cam.SetExtents(span, span)

    def view_images(self, views=("iso",), width: int = 800, height: int = 600,
                    white: bool = False, focus: str = "", sketches: bool = False,
                    up="y", isolate: str = "", look_from=None, target=None,
                    zoom: float = 1.0, perspective: bool = False) -> list[str]:
        """Render the active document from each named view into PNG files and return their
        paths. The camera is put back afterwards, so the user's view doesn't jump.
        focus: occurrence paths, ","-separated — zoom to them instead of the whole model.
        isolate: occurrence paths — hide every other part while capturing.
        look_from / target: mm, model coordinates — the view named "custom"; up: "x"/"y"/"z"
        or a vector. zoom: 2 = twice as close, every view."""
        import tempfile
        unknown = [v for v in views if v not in self.VIEWS and v != "custom"]
        if unknown:
            raise ValueError(f"Unknown view {', '.join(unknown)} — use {', '.join(self.VIEWS)}, "
                             "or custom with look_from")
        if "custom" in views and look_from is None:
            raise ValueError("The custom view needs look_from=[x, y, z]")
        if zoom <= 0:
            raise ValueError("zoom must be above 0")
        self.active_document()
        view = self.app.ActiveView
        cam = view.Camera
        saved = (cam.Eye, cam.Target, cam.UpVector, cam.Perspective)
        try:   # hiding sketches and components for the picture sets the flag; nothing was edited
            clean = not self.app.ActiveDocument.Dirty
        except Exception:
            clean = False
        box = self._occurrence_box(focus) if focus else None
        hidden = self._isolate(isolate) if isolate else []
        colors = ()
        if white:
            colors = (self.app.TransientObjects.CreateColor(255, 255, 255),) * 2  # top, bottom
        folder = tempfile.mkdtemp(prefix="inventor-view-")
        paths = []
        shown = {}
        if not sketches:  # sketches and work features hide the shape in a picture
            vis = self.app.ActiveDocument.ObjectVisibility
            for prop in self._CLUTTER:
                try:
                    shown[prop] = getattr(vis, prop)
                    setattr(vis, prop, False)
                except Exception:
                    pass
        try:
            for name in views:
                custom = name == "custom"
                if custom:
                    cam.Perspective = bool(perspective)
                    self._custom_view(cam, look_from, target, up, box)
                    cam.Apply()
                elif name != "current":
                    if isinstance(up, str) and up.lower() == "z":
                        self._z_up_view(cam, name)
                    else:
                        cam.ViewOrientationType = self.VIEWS[name]
                    cam.Perspective = False
                    cam.Fit()
                    cam.Apply()
                if box is not None and not (custom and target is not None):
                    self._zoom_to(cam, box)
                if zoom != 1:
                    w, h = cam.GetExtents()
                    cam.SetExtents(w / zoom, h / zoom)
                    cam.Apply()
                view.Update()  # the camera change has to be drawn before the picture is taken
                path = os.path.join(folder, f"{name}.png")
                cam.SaveAsBitmap(path, int(width), int(height), *colors)
                paths.append(path)
        finally:
            for o in hidden:
                try:
                    o.Visible = True
                except Exception:
                    pass
            cam.Eye, cam.Target, cam.UpVector = saved[0], saved[1], saved[2]
            cam.Perspective = saved[3]
            cam.Apply()
            for prop, value in shown.items():
                try:
                    setattr(self.app.ActiveDocument.ObjectVisibility, prop, value)
                except Exception:
                    pass
            if clean:
                try:
                    if self.app.ActiveDocument.Dirty:
                        self.app.ActiveDocument.Dirty = False
                except Exception:
                    pass
        return paths

    def _occurrence_box(self, names: str):
        """Range box around the named occurrences (top assembly coordinates)."""
        boxes = [self._occ(n.strip()).RangeBox for n in names.split(",") if n.strip()]
        box = boxes[0].Copy()
        for b in boxes[1:]:
            box.Extend(b.MinPoint)
            box.Extend(b.MaxPoint)
        return box

    def _isolate(self, names: str) -> list:
        """Hide every part outside the named occurrences; returns what to show again."""
        keep = [n.strip() for n in names.split(",") if n.strip()]
        for n in keep:
            self._occ(n)  # a wrong name fails here, before anything is hidden
        hidden = []
        leaves = self.active_assembly().Occurrences.AllLeafOccurrences
        for i in range(1, leaves.Count + 1):
            o = leaves.Item(i)
            path = self._path_of(o)
            if o.Visible and not any(path == k or path.startswith(k + "/") for k in keep):
                o.Visible = False
                hidden.append(o)
        return hidden

    @staticmethod
    def _zoom_to(cam, box):
        """Keep the view direction, center on the box and frame it with some margin."""
        lo, hi = box.MinPoint, box.MaxPoint
        center = [(a + b) / 2 for a, b in ((lo.X, hi.X), (lo.Y, hi.Y), (lo.Z, hi.Z))]
        size = max(hi.X - lo.X, hi.Y - lo.Y, hi.Z - lo.Z) * 1.3
        eye, target = cam.Eye, cam.Target
        d = [eye.X - target.X, eye.Y - target.Y, eye.Z - target.Z]
        t = cam.Target
        t.X, t.Y, t.Z = center
        e = cam.Eye
        e.X, e.Y, e.Z = (c + v for c, v in zip(center, d))
        cam.Target, cam.Eye = t, e
        cam.SetExtents(size, size)
        cam.Apply()
