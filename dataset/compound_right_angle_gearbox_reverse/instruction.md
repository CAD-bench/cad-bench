Create a compound right-angle gearbox that reverses output direction.

Hidden evaluation mounts the model onto three fixed perpendicular shafts and runs a rigid-body physical simulation.
Model only the gears, not the shafts or housing.

Shaft layout:
- Input shaft: global Z axis through (x, y) = (12, 0)
- Compound shaft: global X axis through (y, z) = (0, 18)
- Output shaft: global Y axis through (x, z) = (40, 18)

Gear requirements:
- Four separate gears total:
  - one gear on the input shaft
  - two gears on the compound shaft
  - one gear on the output shaft
- Keep the whole model inside:
  - x in [4, 48]
  - y in [-11, 11]
  - z in [7, 29]
- Approximate outer diameters:
  - input bevel gear: 15 mm
  - compound stage-1 bevel gear: 21 mm
  - compound stage-2 bevel gear: 11 mm
  - output bevel gear: 15 mm
- Use a normal bevel-gear style layout with matched pitch-cone geometry for both right-angle stages
- The compound shaft should carry two distinct gears centered near x=17 and x=35
- Make the first right-angle stage visibly larger than the second stage
- The authored gear bodies must be disjoint at rest: zero body-body intersections
- Under the physical simulation, the design should produce a visible reduction with output speed around -15 rpm from a +30 rpm input
- The intended arrangement is a two-stage right-angle transfer with reversed output direction

Scoring points:
- correct shaft assignment and 4-gear compound structure
- right-angle layout on the expected axes and shaft centers
- stage sizes and placement consistent with a stable visible reduction
- zero intersecting solids in the authored initial state
- physical transfer under Blender rigid-body simulation
- output-stage arrangement consistent with the reversed-direction design intent

Submission contract

Create `/workspace/final.py`. The file must run with Python 3.11 and Build123D 0.10.0, and it must leave the completed model in a top-level variable named `part`. The verifier evaluates that object directly.
