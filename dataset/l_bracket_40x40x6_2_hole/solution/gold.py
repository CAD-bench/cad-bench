from build123d import *

arm_x = Box(40.0, 20.0, 6.0, align=(Align.MIN, Align.MIN, Align.MIN))
arm_y = Box(20.0, 40.0, 6.0, align=(Align.MIN, Align.MIN, Align.MIN))
part = arm_x + arm_y
part = part - Cylinder(3.0, 6.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((10.0, 10.0, 0.0)))
part = part - Cylinder(3.0, 6.0, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((30.0, 10.0, 0.0)))
