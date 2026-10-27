# Inventor 2026 / 2027 COM API — Field Notes

Empirically verified knowledge from driving Autodesk Inventor through `pywin32`
dynamic dispatch. Much of this contradicts the official documentation or is
documented nowhere. Every item here was discovered by hitting the error first.

## Inventor 2027 status

Everything below was re-verified against **Inventor 2027** (SoftwareVersion.DisplayVersion
`2027`, type library GUID `{D98A091D-3A0F-4C3E-B36E-61F62068D488}` v1.0) with
`tests/smoke_test.py`: 57/57 steps pass, no code changes required.

- All enum constants used by this project are **unchanged** from 2026 — verified against
  the live type library, not just by things-still-work.
- `ChamferFeatures.AddUsingDistance`, `HoleFeatures.CreateSketchPlacementDefinition` +
  `CreateTapInfo("ISO Metric profile", ...)`, `FaceFeatures.CreateFaceFeatureDefinition`,
  `FlangeFeatures.CreateFlangeDefinition`, `CutFeatures.CreateCutDefinition` and
  `CircularPatternFeatures.Add(..., 47363)` all behave as documented here.
- The sheet-metal flange quirk (Distance argument silently ignored, angle in radians)
  **still applies in 2027** — the workaround via
  `feat.Definition.HeightExtent.Distance.Expression` is still required.

Reading enum values without poisoning the `gen_py` cache (see below): load the type
library directly with `pythoncom.LoadRegTypeLib` and walk `TKIND_ENUM` type infos —
`tests/smoke_test.py:check_enums` does exactly this and is the recommended way to
qualify a new Inventor release.

## Enum values (Inventor 2026 — identical in 2027)

```python
# DocumentTypeEnum
kPartDocumentObject = 12290     # NOT 12289 as often quoted!

# PartFeatureExtentDirectionEnum
kPositiveExtentDirection  = 20993
kNegativeExtentDirection  = 20994
kSymmetricExtentDirection = 20995

# PartFeatureOperationEnum
kJoinOperation      = 20481
kCutOperation       = 20482
kIntersectOperation = 20483
kSurfaceOperation   = 20484
kNewBodyOperation   = 20485

# DimensionOrientationEnum — docs say 40706/40707/40708; reality:
kHorizontalDim = 19201
kVerticalDim   = 19202
kAlignedDim    = 19203

# SurfaceTypeEnum (Face.SurfaceType)
# 5890=Plane, 5891=Cylinder, 5892=EllipticCylinder, 5893=Cone, 5894=EllipticCone,
# 5895=Torus, 5896=Sphere, 5897=BSpline  (earlier notes here had 5892–5894 wrong)

# HealthStatusEnum (feature.HealthStatus, constraint.HealthStatus)
# 11778=UpToDate, 11779=OutOfDate, 11780=DriverLost, 11781=InError, 11782=Deleted,
# 11783=CannotCompute, 11784=Suppressed, 11785=BeyondStopNode, 11786=Inconsistent,
# 11787=Redundant — the server reads the names from the type library
# (inventor_api.health_name) and falls back to this table

# AngleConstraintSolutionTypeEnum (4th argument of AddAngleConstraint)
# 78593=kDirectedSolution
```

## Internal units are CENTIMETERS

All COM API geometry is in cm. Divide millimeters by 10 everywhere.
Angles are radians in most (but not all) places — see Flange below.

## gencache breaks GetActiveObject

`win32com.client.gencache.EnsureDispatch("Inventor.Application")` generates a
`gen_py` typed cache. After that, plain `GetActiveObject` fails with
`KeyError: '_dispobj_'` **until you delete `%LOCALAPPDATA%\Temp\gen_py`**.

But gencache is invaluable for one-off API discovery — typed signatures show
arguments that dynamic dispatch can't tell you about. Workflow:
1. Run a probe script with `EnsureDispatch` + `CastTo` in a separate process
2. Record the signatures you need
3. Delete `gen_py`, verify `GetActiveObject` works again
4. Continue with dynamic dispatch

With typed bindings, sheet-metal collections require casts:
```python
smf  = CastTo(comp.Features, "SheetMetalFeatures")
smcd = CastTo(part_doc.ComponentDefinition, "SheetMetalComponentDefinition")
```
(Dynamic dispatch reaches `comp.Features.FlangeFeatures` directly — no cast.)

**The server is immune (0.4.1+).** A cache also appears when an AI session calls `CastTo` in
`execute_python` — typed objects make it look necessary: a `Document` from `Documents.Open` is
typed as the generic `Document` interface and hides `ComponentDefinition`. `inventor_api.py` sets
`win32com.client.gencache.GetClassForCLSID = lambda clsid: None` at import, so pywin32 never wraps
anything in a gen_py class in the server process: `GetActiveObject` works with a cache present, and
every object is late-bound *with* type info (`Item(1)` etc. still work). Two things that don't work
instead: `win32com.client.dynamic.Dispatch(obj._oleobj_)` (methods with type info still wrap their
results through gencache) and `DumbDispatch` (no type info → parameterized properties like
`Documents.Item` raise "member not found").

## Modal dialogs block API calls

An API call that makes Inventor open a dialog does not return until someone answers it — e.g.
`Documents.Open` of an assembly whose reference was resolved to a different file ("Do you really
want to use the resolved document?"), or `Save` with dirty dependents ("save dependents?").
`app.SilentOperation = True` makes Inventor take the default answer instead; the server sets it
around every call. A modal dialog disables Inventor's main window (`app.MainFrameHWND`), so a stuck
call is diagnosable without COM: the dialog is a visible, enabled top-level window of the same
process (`win32gui.EnumWindows`), and its child windows carry the message text.

## Several Inventor processes

Each Inventor registers itself in the Running Object Table under the same moniker
(`!{B6B5DC40-96E3-11D2-B774-0060B0F159EF}`). `GetActiveObject("Inventor.Application")` — and even
walking the ROT and binding each entry — returns the FIRST registered instance every time, so a
second Inventor opened by hand can't be reached. Open documents are not in the ROT either.
`DispatchEx("Inventor.Application")` always starts a new process and returns it; keep that reference
(the process outlives it, but nothing can find it again). When the first Inventor quits, the next one
moves up in the ROT.

`DesignProject.Activate(False)` switches the project for this session only — the default project of
the next Inventor start stays as it was. Inventor refuses to switch while any document is open.

## Content Center

- `app.ContentCenter` raises E_FAIL until `app.Ready` is True — wait after starting Inventor.
- Walking the whole tree (`TreeViewTopNode` → `ChildNodes` / `Families`) takes minutes. Navigate by
  `DisplayName` path ("Structural Shapes/Channels") and cache the nodes; listing one node's 24
  families takes ~3 s.
- Columns: `DESIGNATION` / `G_D` / `SIZE` name the size, `B_L` is the length of structural shapes.
  Cell values come back as locale strings ("10,6").
- Custom length: `ContentFamily.CreateMember(row, 0, "", 51969, True, full_path, NameValueMap{"B_L": mm})`
  (51969 = kUseDefaultRefreshSetting); the length is in mm. Returns the file path.

## Manufacturing outputs

- Flat pattern DXF: `SheetMetalComponentDefinition.DataIO.WriteDataToFile("FLAT PATTERN DXF?AcadVersion=2018&OuterProfileLayer=OUTER&InteriorProfilesLayer=INNER&InvisibleLayers=IV_TANGENT;…", path)`.
  `HasFlatPattern` / `Unfold()` (then `FlatPattern.ExitEdit()`) creates it; `FlatPattern.Length/Width` in cm.
- BOM: enable `BOM.PartsOnlyViewEnabled` / `StructuredViewEnabled` (+ `StructuredViewFirstLevelOnly = False`),
  pick the view by `ViewType` (62466 structured, 62467 parts only — names are localized). Row →
  `ComponentDefinitions.Item(1).Document` → "Design Tracking Properties".
- Material: `doc.ActiveMaterial = libraryAsset.CopyTo(doc)` (library: `app.AssetLibraries`).
- Drawing: `FileManager.GetTemplateFile(kDrawingDocumentObject, …)` returned "" here — glob
  `TemplatesPath/**/Metric/ISO.idw`. `AddBaseView(model, point, scale, orientation, 32258, "", "", options)`;
  `options["SheetMetalFoldedModel"] = False` for the flat pattern. `AddProjectedView(base, point, style[, scale])`
  — the direction comes from the position. Overall dimensions: `GeneralDimensions.AddLinear(point,
  sheet.CreateGeometryIntent(curve1), …(curve2), 60162 horizontal / 60163 vertical)`.
- PDF translator `{0AC6FD96-2F4D-42CE-8BE0-8AEA580399E4}`. `NameValueMap.Value(name)` is a property
  with a parameter: late-bound pywin32 can read it but not set it — use `Add`, or invoke
  `DISPATCH_PROPERTYPUT` on its DISPID.
- `DispatchEx` can hand back an Inventor that is already running (one left half-quit by a client
  that exited right after `Quit()`) instead of starting a new one — and calls on it can hang.

## Frame Generator

No API: `ApplicationAddIns.ItemById("{AC211AE0-A7A5-4589-916D-81C529DA6D17}").Automation` is None after
`Activate()`, the type library has no Frame Generator classes (only `FrameGeneratorFileNamingOptions`),
and its commands (`AFG_CMD_InsertProfile`, `AFG_Mitrecut_Profiles`, …) need interactive selection.
`frame_members` places Content Center custom-length members instead. A member part's length axis is
found from its range box (the axis whose extent equals the length); its section center from the
other two.

## Sketch arcs

- `SketchArcs.AddByThreePoints(start, mid, end)` takes SketchPoints for start and end, so a chain of
  lines and arcs can share its points (one closed loop). Inventor stores arcs counter-clockwise: a
  clockwise three-point arc comes back with start and end swapped — match the next segment to the end
  point nearest the intended one.
- `AddByCenterStartEndPoint`'s counter-clockwise flag is read in sketch space; on a face sketch with
  flipped axes the arc came out mirrored (a thin strip instead of an oval). Three points avoid it.
- A tangent constraint between a line and an arc that isn't tangent makes the solver bend the
  geometry — a D shape (line + arc through a point) collapsed to a single point. Constrain only
  rounded corners.

## Sketch offset and many copies

- `PlanarSketch.OffsetSketchEntitiesUsingDistance(entities, distance, NaturalOffsetDirection,
  IncludeConnectedEntities, CreateOffsetConstraints)`: hand it ONE curve of a closed loop with
  IncludeConnectedEntities = True. The whole loop curve by curve fails ("Failed to create offset")
  either way. Which side NaturalOffsetDirection means isn't documented — offset, measure, and redo
  the other way if needed.
- `PlanarSketch.DeferUpdates = True` while drawing many curves, then False.
- `Profiles.AddForSolid()` on an open sketch raises a bare E_INVALIDARG; Inventor's ErrorManager says
  "Failed to build a profile from sketch". Check shared end points to say where.

## Pictures

`View.SaveAsBitmap(path, width, height)` takes no colors; `Camera.SaveAsBitmap(path, width, height,
TopColor, BottomColor)` does (both colors, e.g. white for a plain background). Set
`Camera.ViewOrientationType` (ViewOrientationTypeEnum: iso top-right 10759, front 10764, top 10754,
right 10755, …), `Fit()`, `Apply()`, save, then put Eye/Target/UpVector back. 800×600 takes ~0.6 s.
`Document.ObjectVisibility.Sketches` / `UserWorkPlanes` / `OriginWorkPlanes` … hide the clutter.

## Reaching a second Inventor again

The ROT lists every Inventor under the same moniker and binding returns the first, so a reference
to a second Inventor is the only way to it. Registering that reference under a new name fails:
`IRunningObjectTable.Register(flags, inventorProxy, itemMoniker)` → E_INVALIDARG, with either
flag and IUnknown or IDispatch. Registering an object that lives in the registering process works,
and its methods can return the Inventor proxy — another process then calls Inventor directly. The
registering process has to pump messages; a parent that blocks waiting on a child that calls back
into it deadlocks (`subprocess.run` + an in-process object).

## Processes left behind

An Inventor started with `DispatchEx` keeps running after the client process exits. After
`Quit()`, release the reference and wait a few seconds before exiting — a client that exits right
after `Quit()` can leave a windowless `Inventor.exe` behind, which no client can reach again.

## Chamfer — API changed in 2026

`CreateChamferDefinition()` was **removed**. Use:
```python
ec = app.TransientObjects.CreateEdgeCollection()   # EdgeCollection, NOT ObjectCollection!
ec.Add(body.Edges.Item(i))
comp.Features.ChamferFeatures.AddUsingDistance(ec, dist_cm)
# also: AddUsingDistanceAndAngle, AddUsingTwoDistances
```
Put ALL edges in ONE call — edge indices renumber after each feature.

## Revolve

- Full 360°: `RevolveFeatures.AddFull(profile, axis_entity, operation)`
- Partial: `AddByAngle(profile, axis, angle_RAD, direction, operation)`
- Preferred axis: a WorkAxis (`comp.WorkAxes.Item(1/2/3)` = X/Y/Z) — no
  construction line needed in the sketch.
- Do NOT use `CreateRevolveDefinition` + `SetAngleExtent`.

## HoleFeature

`CreateSimpleHoleDef` doesn't exist in 2026. Patterns that work:

```python
# Sketch-point placement
col = app.TransientObjects.CreateObjectCollection()   # of SketchPoints
col.Add(sketch_point)
pl = hf.CreateSketchPlacementDefinition(col)
hf.AddDrilledByThroughAllExtent(pl, diameter_cm_or_TapInfo, direction)
hf.AddDrilledByDistanceExtent(pl, dia_or_tap, depth_cm, direction)

# Edge-referenced (parametric) placement — survives part resizing
pl = hf.CreateLinearPlacementDefinition(face, edge1, d1_cm, edge2, d2_cm,
                                        bias_point)   # Point3d — REQUIRED
# bias_point disambiguates among 4 possible positions; compute it by stepping
# from each reference edge toward the face centroid.

# Counterbore (e.g. M5 socket head cap screw seat: Ø5.5 hole, Ø10×5.5 seat)
hf.AddCBoreByDistanceExtent(pl, 0.55, depth_cm, direction, 1.0, 0.55)
```

Gotchas:
- The hole-placement sketch must contain ONLY `SketchPoints.Add()` — a single
  construction line in it makes `AddDrilledByThroughAllExtent` fail with E_FAIL.
- Drill direction (positive/negative) into material is not predictable from the
  face normal. Create the feature, check `MassProperties.Volume` dropped, flip
  direction and retry if it didn't.
- Bodies extruded in the "negative" direction can make through-all holes
  silently remove nothing. Extrude bodies positive.

### Tapped holes (TapInfo)

```python
tap = hf.CreateTapInfo(True, "ISO Metric profile", "M16x2", "6H", True)
```
- ThreadType must be the **exact** sheet name from
  `Design Data\XLS\en-US\thread.xlsx` (e.g. `"ISO Metric profile"`).
- Designation: integer pitches WITHOUT decimal — `"M16x2"`, not `"M16x2.0"`.
- TapInfo works with both through-all and distance extents.

## Sketch dimensions

- `DimensionConstraints.AddDiameter(circle, textPt)` — straightforward.
- `AddTwoPointDistance(p1, p2, orientation, textPt)` — the points must be
  **line endpoints or projected points**; standalone `SketchPoints.Add()` gives
  E_UNEXPECTED (0x8000FFFF).
- Project the origin into the sketch to dimension from it:
  `sketch.AddByProjectingEntity(comp.WorkPoints.Item(1))`.
- Project axes as construction lines for symmetry/coincident constraints:
  result is collection-like — take `.Item(1)`, set `.Construction = True`.
- Prefer `GeometricConstraints.AddCoincident(center, projected_axis)` over
  zero-value dimensions; `AddSymmetry(l1, l2, axis_line)` to center base sketches.

## Features — what can't be done or changed

- **`FilletFeatures.AddSimple`** follows tangent chains: one edge picks up every
  tangent-continuous neighbour. The order of fillets matters — a fillet changes the
  edges the next one finds; fillet the large radii first.
- **`KnitFeatures.Add` failed with E_FAIL** on imported (STEP) surface bodies in every
  form tried. Repair or stitch them in the source, or import as a solid.
- **`FaceFeature.Definition` can't be changed afterwards** — delete and recreate.
- **`ContourFlangeDefinition` has no flip.** The material side follows the profile's
  direction: draw the profile the other way round to put the material on the other side.
- A feature-level bend radius works as an expression string: `BendRadius = "0.8 mm"`.

## Sheet Metal (2026)

### Activating a sheet metal rule

`ComponentDefinition.ActiveSheetMetalStyle` is **read-only** — assigning a style
to it raises `Property ... can not be set`. The style activates itself:

```python
styles = comp.SheetMetalStyles
styles.Item("T15-V10").Activate()      # (by index; match .Name first)
```

A rule carries thickness, bend radius and unfold method together. `Thickness`
and `BendRadius` are stored as *expressions* (`'1,5'`, `'10 / 6'`, `'Thickness'`);
resolve them with `doc.UnitsOfMeasure.GetValueFromExpression(expr, "mm")`
(returns **cm**). After activation the part's `comp.Thickness` Parameter holds
the resolved thickness. Wrapped as `list_sheet_metal_rules` / `set_sheet_metal_rule`.

```python
comp.ActiveSheetMetalStyle.Thickness = "0,8 mm"   # comma decimal on European locales
```

**Face** (base panel): `ff.Add(ff.CreateFaceFeatureDefinition(profile))`.

**Flange** — the big trap:
```python
fdef = flf.CreateFlangeDefinition(edge_collection, angle_RADIANS, distance_cm)
fdef.SetDistanceHeightExtent(distance_cm, kPositiveExtentDirection, 75521)  # ← the real height
fdef.ApplyAutoMitering = True
fdef.MiterGap = "GapSize"          # ← NOT the default, see below
feat = flf.Add(fdef)
doc.Update()
```
- The `distance` argument of `CreateFlangeDefinition` is **silently ignored** —
  the flange is created ~25 mm tall. Set the height with `SetDistanceHeightExtent`
  before `Add` (or afterwards via `feat.Definition.HeightExtent.Distance.Expression`).
- The angle is in **radians**. Pass `90` and Inventor stores 90·180/π ≈ 5157°.
- `HeightDatumType` (`HeightDatumTypeEnum`): 75521 Outer (UI default — the distance
  equals the height from the outer mold line), 75522 Inner, 75523 Tangent,
  75524 OuterOrtho, 75525 InnerOrtho. For a flange bent off another flange the
  "outer intersection" is on the *previous* flange's far face — a 15 mm flange off
  a 1.5 mm return protrudes 13.5 mm past the visible front.
- **Miter gap default differs from the UI.** With several edges in one flange,
  `ApplyAutoMitering` is on by default, but `MiterGap` defaults to the sheet
  **thickness** (expression `'1,500 mm'`), while the UI dialog uses the rule's
  `GapSize`. Shop rules often have GapSize ≈ 0.1 mm, so API-built corners come
  out ~1.5 mm open unless `MiterGap = "GapSize"` is set.
- **Put all edges of a loop in ONE flange** — the corners are then auto-mitered.
  Separate flanges leave open corners that need Corner Seam features.
- **Direction:** the flange bends toward the side of the face the selected edge
  belongs to. Base panel at Z=0..t: the Z=t edges give walls toward +Z, the Z=0
  edges toward −Z. For an inward lip on a wall, pick the wall's inner-face edge.

**Cut**: sketch ON the panel face (not a work plane) → `CreateCutDefinition(profile)`
→ `Add`. Depth then defaults to the Thickness parameter, so the cut stays
correct when thickness changes. One sketch with many closed loops = one Cut
with many holes. Avoid extrude-cuts for sheet metal — they hardcode the depth.
- **A cut sketched on a work plane is created without any error but removes
  nothing** (`+0 mm³`). `sheet_metal_cut` / `cut_on_face` now detect this, delete
  the empty feature and raise.
- Face sketches have their own origin and (often flipped) axes. Place geometry by
  world coordinates with `sketch.ModelToSketchSpace(tg.CreatePoint(x, y, z))`
  (`TransientGeometry.CreatePoint` is the 3D point — there is no `CreatePoint3d`).
  Wrapped as `cut_on_face`.

**Corner Round** (rounded corners that are also in the flat pattern, so the laser
cuts them): `crf.Add(crf.CreateCornerRoundDefinition(edge_collection, "5 mm"))`,
with the short edges running through the thickness at the corners.

**Flat Pattern** is the acid test: if it unfolds, your geometry is real sheet
metal; extrude-built "walls" look identical but won't unfold.

## Assembly (IAM)

```python
asm  = app.Documents.Add(12291, r"...\Metric\Standard (mm).iam", True)  # kAssemblyDocumentObject
comp = asm.ComponentDefinition
occ  = comp.Occurrences.Add(part_path, matrix)   # part must exist ON DISK
occ.Grounded = True
```

- `Occurrences.Add` needs a **saved file** — an unsaved in-memory part cannot be
  placed. Save the .ipt first.
- The matrix is the placement transform; `matrix.SetTranslation(tg.CreateVector(x_cm, y_cm, z_cm))`.
  Skip `SetTranslation` for the identity placement.
- **Ground the first component.** Otherwise the solver satisfies constraints by
  moving the part you consider fixed.
- **Assembly work planes / axes are not grounded.** `comp.WorkPlanes.AddFixed(...)` and
  `comp.WorkAxes.AddFixed(...)` in an assembly create features the solver may move:
  constrain parts to one and the solver moves the plane instead of the parts. Set
  `wp.Grounded = True` right after creating it.

### Constraints need geometry PROXIES

```python
wp    = occ.Definition.WorkPlanes.Item(3)     # part-level entity — WRONG to pass directly
proxy = occ.CreateGeometryProxy(wp)           # assembly-context entity — use this
comp.Constraints.AddMateConstraint(proxy1, proxy2, offset_cm)
comp.Constraints.AddFlushConstraint(proxy1, proxy2, offset_cm)
comp.Constraints.AddAngleConstraint(proxy1, proxy2, angle_RADIANS)
comp.Constraints.AddInsertConstraint(circEdgeProxy1, circEdgeProxy2, axes_opposed, offset_cm)
```

- `CreateGeometryProxy` is documented as taking an out-parameter; under pywin32
  **dynamic dispatch it simply returns the proxy**.
- Angle constraints are in radians, like everything else. Pass the solution type
  explicitly — `AddAngleConstraint(p1, p2, angle_rad, 78593)` (kDirectedSolution) —
  so the angle is measured in a defined direction instead of flipping.
- Prefer origin planes/axes (`WorkPlanes.Item(1/2/3)` = YZ/XZ/XY, `WorkAxes.Item(1/2/3)`
  = X/Y/Z) over picked faces: origin constraints survive part edits, face picks
  break as soon as topology renumbers.
- `AddInsertConstraint` wants **circular edges** — `Edge.GeometryType == 5124`
  (`kCircleCurve`; straight edges are 5123). `edge.Geometry.Center` / `.Radius`
  only exist on circular edges.
- Call `doc.Update()` after adding or deleting a constraint, otherwise
  `occ.Transformation` still reports the pre-solve position.

## Sketch-plane axis mapping

| Plane | sketch X | sketch Y |
|---|---|---|
| XY (WorkPlanes.Item(3)) | world +X | world +Y |
| XZ (Item 2) | **world −X (mirrored!)** | world +Z |
| YZ (Item 1) | world +Y | world +Z |

Offset planes: `comp.WorkPlanes.AddByPlaneAndOffset(base_wp, offset_cm)`;
offset is along the plane normal (XY→+Z, XZ→+Y, YZ→+X). Offset-from-XY planes
map directly (x→X, y→Y). Set `wp.Visible = False`.

**Sketch on a face**: the origin is at a face corner, not the center, and axes
can flip. Always verify with
`sketch.SketchToModelSpace(tg.CreatePoint2d(x_cm, y_cm))` → world Point3d.

## Import / export

- **Find translator add-ins by class ID, not display name.** The names change
  between releases (2027: `"Translator: STEP"`), the IDs don't:
  STEP `{90AF7F40-0C01-11D5-8E83-0010B541CD80}`, IGES `{90AF7F44-0C01-11D5-8E83-0010B541CD80}`,
  SAT `{89162634-02B6-11D5-8E80-0010B541CD80}`, STL export `{533E9A98-FC3B-11D4-8E7E-0010B541CD80}`.
  `app.ApplicationAddIns.ItemById(clsid).SaveCopyAs(doc, context, options, medium)`.
- **Opening a STEP assembly writes its parts next to the .step file**, into a new
  folder named after it (`Downloads/562.1.47/1.ipt ... 7.ipt`). If the STEP sits in
  a synced or shared folder, copy it into the project folder first and open the copy.
  Vendor models are drawn without your sheet thickness: screws, nuts and gaskets often
  overlap the panel — move them to their real seat before judging interference.

## Clearance checks

- `asmDef.AnalyzeInterference(collA, collB)` → results with `Volume` (cm³),
  `OccurrenceOne/Two` and `InterferenceBody` (for the overlap's RangeBox). Touching
  faces are not interference; designed overlaps (gasket compression) are.
- `app.MeasureTools.GetMinimumDistance(a, b)` returns a **tuple** `(distance_cm, context)`.
- A rotation sweep is just `Matrix.SetToRotation(angle, axis, point)` applied to each
  moving occurrence's original `Transformation`, one interference check per step —
  restore the transformations afterwards.

## Documents and saving

- **`Document.Save()` can block on a modal dialog.** When a referenced file was
  updated (an assembly whose sub-assembly re-evaluated, a part edited in
  context), Inventor opens "Do you want to save changes to ... and its
  dependents?" — even when called through the API — and the call waits until a
  user clicks OK. Use `Save2` with `app.SilentOperation = True` around it, and
  restore the flag afterwards.
- **`Save2(True)` saves every dirty dependent — library files included.** Opening an
  old assembly can mark its standard Content Center parts dirty (Inventor recomputes
  them), and `Save2(True)` then rewrites those shared files. `save_document` instead
  saves each dirty dependent with `Save2(False)`, skipping files under the active
  project's `LibraryPaths`, its `ContentCenterPath`, `app.FileOptions.ContentCenterPath`
  and any part whose `ComponentDefinition.IsContentMember` is True, then the document
  itself with `Save2(False)`. (`Save2(True, objectCollection)` would also limit the
  set, but one failing file fails the whole call.) `ProjectPath.Path` can be relative
  to the .ipj's folder.
- **`Documents.Add(type, template, True)` does not reliably activate the new
  document.** `app.ActiveDocument` can keep returning the previous one, so code
  that works on "the active document" edits the wrong file. Call `doc.Activate()`
  after creating, and prefer finding documents by `FullFileName` over relying on
  `ActiveDocument` in scripts.
- Documents opened as references of an assembly are invisible; `Activate()` on
  them fails — open them visibly with `app.Documents.Open(path, True)` first.
- **`Documents.Open(path, False)` documents can disappear.** An invisible document
  nothing references can be closed by Inventor later (e.g. when another document
  closes); a variable holding it then raises RPC_E_DISCONNECTED. Keep a visible
  reference, or reopen by path before each use.

## Stale objects and collections

- **COM objects go stale.** After a document is closed and reopened, or a feature
  deleted (also by an undo), objects fetched before raise `RPC_E_DISCONNECTED`
  (0x80010108, "the object invoked has disconnected from its clients"). Re-resolve
  them from `app` / `doc` — don't keep them in the `execute_python` namespace across
  such steps.
- **Collections can lag.** E.g. `comp.RepresentationsManager.PositionalRepresentations`
  can return the old list right after `.Add` on an object fetched before. Re-query
  the collection from its owner on each use.
- **Assembly MassProperties can be stale** until the document is updated: the
  value cached in the file comes back (`CacheResultsOnCompute`). Call `doc.Update()`
  and set `mp.CacheResultsOnCompute = False` before reading — `assembly_info` does.

## Tuple-returning calls

Out-parameters come back as a tuple under pywin32 dynamic dispatch:

- `app.MeasureTools.GetMinimumDistance(a, b)` → a tuple; `[0]` is the distance in cm.
- `CurveEvaluator.GetStrokes(fromParam, toParam, tolerance)` needs the parameter
  range — take it from `ev.GetParamExtents()` (a tuple: min, max) — and returns
  (vertex count, flat coordinate list in cm).
- `SurfaceBody.FindUsingRay(point, direction, radius)` returns the hit objects and
  points as a tuple, not through the arguments.

## Geometry queries

- **`RangeBox` is loose on curved faces** — about 0.25 mm on elliptic and B-spline
  faces (planar and cylindrical ones are tight). Don't use it for exact sizes; measure
  with `MeasureTools` or evaluate the surface.

## Topology pitfalls

- **Edge/face indices renumber after every feature.** Re-find by midpoint /
  centroid coordinates, never cache indices across features.
- **Fillets shift adjacent edges** — a fillet extends/shortens its neighbours,
  moving their midpoints. Re-scan topology after every fillet.
- **Feature order vs cuts**: material added after a cut fills the cut where
  they overlap. Adding a rib under an already-bored hub? Stop it below the
  bore's bottom, or reorder the feature tree.

## Misc

- `doc.SaveAs(path)` fails with E_INVALIDARG if the file is already open under
  that name — use `doc.Save()` for re-saves.
- Inventor numbers sketches/features globally and never reuses deleted numbers.
  Rename features (`feat.Name = "Bore20"`) for reliable references.
- Console scripts printing Unicode (Ø, ×, Cyrillic) need `python -X utf8` on
  Windows (cp1251/cp1252 consoles).
- `app.TransactionManager.StartTransaction(doc, "name")` → `.End()` / `.Abort()`
  wraps multi-feature operations into one undo unit with reliable rollback.
