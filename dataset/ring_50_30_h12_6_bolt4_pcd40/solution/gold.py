from build123d import *
import math

part = Cylinder(25.0, 12.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
part = part - Cylinder(15.0, 12.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
for i in range(6):
    a = math.radians(60.0 * i)
    x = 20.0 * math.cos(a)
    y = 20.0 * math.sin(a)
    part = part - Cylinder(2.0, 12.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((x, y, 0.0)))
