from build123d import *

ROOT_D = 13.5
OUTER_D = 18.0
THICK = 6.0
TEETH = 16
TOOTH_H = (OUTER_D - ROOT_D) / 2.0
TOOTH_W = 1.8
R_MID = (OUTER_D + ROOT_D) / 4.0

core = Cylinder(ROOT_D / 2.0, THICK, align=(Align.CENTER, Align.CENTER, Align.MAX))
with BuildPart() as teeth:
    with PolarLocations(R_MID, TEETH):
        Box(TOOTH_H, TOOTH_W, THICK, align=(Align.CENTER, Align.CENTER, Align.MAX))

part = core + teeth.part
part = part - Cylinder(2.5, THICK + 0.2, align=(Align.CENTER, Align.CENTER, Align.MAX))
