from build123d import *

AF = 17.0
corner_r = AF / (3 ** 0.5)

with BuildPart() as nut:
    with BuildSketch(Plane.XY):
        RegularPolygon(radius=corner_r, side_count=6)
    extrude(amount=8.0)

part = nut.part - Cylinder(5.0, 8.2, align=(Align.CENTER, Align.CENTER, Align.MIN))
