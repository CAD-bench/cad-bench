from build123d import *

body = Box(20.0, 20.0, 60.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
cuts = [Cylinder(2.5, 60.0, align=(Align.CENTER, Align.CENTER, Align.MIN))]
for x, y in ((7.0, 0.0), (-7.0, 0.0), (0.0, 7.0), (0.0, -7.0)):
    cuts.append(Cylinder(1.5, 60.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((x, y, 0))))

part = body
for cut in cuts:
    part = part - cut
