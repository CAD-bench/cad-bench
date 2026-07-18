from build123d import *

part = Cylinder(15.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
part = part - Cylinder(10.0, 10.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
