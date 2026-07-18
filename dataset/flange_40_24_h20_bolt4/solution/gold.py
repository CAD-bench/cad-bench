from build123d import *

base = Cylinder(20.0, 8.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
boss = Cylinder(12.0, 12.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((0, 0, 8.0)))
part = base + boss

part = part - Cylinder(6.0, 20.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
for x, y in ((15.0, 0.0), (-15.0, 0.0), (0.0, 15.0), (0.0, -15.0)):
    part = part - Cylinder(2.5, 20.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((x, y, 0.0)))
