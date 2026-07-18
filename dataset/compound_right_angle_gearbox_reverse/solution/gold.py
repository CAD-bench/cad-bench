from math import atan, cos, pi, radians, sin, tan
from build123d import *

AXLE_R = 2.2
CLEAR = 0.0

def d_bore(length: float, z0: float, z1: float):
    mid = 0.5 * (z0 + z1)
    cutter = Cylinder(AXLE_R, length + 0.8, align=(Align.CENTER, Align.CENTER, Align.CENTER)).located(
        Location((0.0, 0.0, mid))
    )
    flat = Box(AXLE_R * 1.6, AXLE_R * 1.6, length + 1.0, align=(Align.MIN, Align.CENTER, Align.CENTER)).located(
        Location((0.4, 0.0, mid))
    )
    return cutter - flat

def gear_profile(outer_r: float, root_r: float, teeth: int, phase_deg: float = 0.0):
    pitch = 2.0 * pi / max(1, teeth)
    tooth_frac = 0.45
    phase = radians(phase_deg)
    pts = []
    for idx in range(teeth):
        center = phase + idx * pitch
        tooth_half = 0.5 * tooth_frac * pitch
        gap_half = 0.5 * pitch
        a0 = center - gap_half
        a1 = center - tooth_half
        a2 = center + tooth_half
        a3 = center + gap_half
        pts.extend(
            [
                (root_r * cos(a0), root_r * sin(a0)),
                (outer_r * cos(a1), outer_r * sin(a1)),
                (outer_r * cos(a2), outer_r * sin(a2)),
                (root_r * cos(a3), root_r * sin(a3)),
            ]
    )
    return pts

def bevel_from_pitch(
    teeth_self: int,
    teeth_mate: int,
    t0: float,
    t1: float,
    axis_name: str,
    origin: tuple[float, float, float],
    phase_deg: float = 0.0,
):
    delta = atan(teeth_self / teeth_mate)
    outer_r0 = max(AXLE_R + 0.22, t0 * tan(delta) - CLEAR)
    outer_r1 = max(outer_r0 + 0.6, t1 * tan(delta) - CLEAR)
    root_r0 = max(AXLE_R + 0.06, outer_r0 - 0.42)
    root_r1 = max(root_r0 + 0.2, outer_r1 - 0.72)
    with BuildPart() as gear_bp:
        with BuildSketch(Plane.XY.offset(t0)):
            Polygon(*gear_profile(outer_r0, root_r0, teeth_self, phase_deg=phase_deg), align=Align.NONE)
        with BuildSketch(Plane.XY.offset(t1)):
            Polygon(*gear_profile(outer_r1, root_r1, teeth_self, phase_deg=phase_deg), align=Align.NONE)
        loft()
    gear = gear_bp.part - d_bore(t1 - t0, t0, t1)
    if axis_name == "x+":
        gear = gear.rotate(Axis.Y, 90.0)
    elif axis_name == "x-":
        gear = gear.rotate(Axis.Y, -90.0)
    elif axis_name == "y+":
        gear = gear.rotate(Axis.X, -90.0)
    return gear.located(Location(origin))

# Stage 1 uses matched complementary pitch cones around the (12, 0, 18) apex.
z0 = 4.6
z1 = 10.6
x0 = z0 * tan(atan(14.0 / 20.0))
x1 = z1 * tan(atan(14.0 / 20.0))
input_gear = bevel_from_pitch(14, 20, z0, z1, "z", (12.0, 0.0, 18.0), phase_deg=0.0)
compound_stage1 = bevel_from_pitch(20, 14, x0, x1, "x+", (12.0, 0.0, 18.0), phase_deg=6.0)

# Stage 2 repeats the same construction around the (40, 0, 18) apex.
x2_0 = 3.0
x2_1 = 7.5
y0 = x2_0 * tan(atan(12.0 / 16.0))
y1 = x2_1 * tan(atan(12.0 / 16.0))
compound_stage2 = bevel_from_pitch(12, 16, x2_0, x2_1, "x-", (40.0, 0.0, 18.0), phase_deg=0.0)
output_gear = bevel_from_pitch(16, 12, y0, y1, "y+", (40.0, 0.0, 18.0), phase_deg=10.0)

part = Compound([input_gear, compound_stage1, compound_stage2, output_gear])
