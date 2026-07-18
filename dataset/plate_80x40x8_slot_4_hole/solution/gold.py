from build123d import *

part = Box(80.0, 40.0, 8.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
part = part - Box(40.0, 10.0, 8.2, align=(Align.CENTER, Align.CENTER, Align.MIN))
for x, y in ((30.0, 15.0), (30.0, -15.0), (-30.0, 15.0), (-30.0, -15.0)):
    part = part - Cylinder(3.0, 8.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((x, y, 0.0)))
