from build123d import *

part = Box(100.0, 60.0, 12.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
part = part - Box(50.0, 12.0, 12.2, align=(Align.CENTER, Align.CENTER, Align.MIN))
for x, y in ((40.0, 20.0), (40.0, -20.0), (20.0, 20.0), (20.0, -20.0), (-20.0, 20.0), (-20.0, -20.0), (-40.0, 20.0), (-40.0, -20.0)):
    part = part - Cylinder(3.0, 12.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((x, y, 0.0)))
