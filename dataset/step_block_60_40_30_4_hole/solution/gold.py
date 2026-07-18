from build123d import *

base = Box(60.0, 40.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
mid = Box(40.0, 30.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((0.0, 0.0, 10.0)))
top = Box(20.0, 20.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((0.0, 0.0, 20.0)))
part = base + mid + top
for x, y in ((15.0, 10.0), (15.0, -10.0), (-15.0, 10.0), (-15.0, -10.0)):
    part = part - Cylinder(3.0, 30.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((x, y, 0.0)))
