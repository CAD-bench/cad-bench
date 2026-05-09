from math import atan2, cos, pi, radians, sin
from build123d import *

AXLE_Z = 10.0
AXLE_R = 2.0
BORE_FLAT_X = 1.5
PRESSURE_ANGLE_DEG = 20.0
ADDENDUM_FACTOR = 0.85
TOOTH_FRACTION = 0.40
TEETH_A = 11
TEETH_B = 15
TEETH_C = 24
MODULE = 40.0 / (TEETH_A + TEETH_B)
PHASE_B = 0.0
PHASE_C = 0.0

def rotate_point(point, angle):
    x, y = point
    c = cos(angle)
    s = sin(angle)
    return (x * c - y * s, x * s + y * c)

def flip_y(point):
    x, y = point
    return (x, -y)

def involute_tooth_and_gap(module, teeth, root_radius, pressure_angle_deg=PRESSURE_ANGLE_DEG, max_steps=72):
    pressure_angle = radians(pressure_angle_deg)
    pitch_radius = module * teeth / 2.0
    base_radius = cos(pressure_angle) * pitch_radius
    outer_radius = pitch_radius + ADDENDUM_FACTOR * module
    theta_tooth_and_gap = 2.0 * pi / teeth
    theta_tooth = theta_tooth_and_gap * TOOTH_FRACTION

    half_tooth = []
    theta_pitch_intersect = None
    theta_full_tooth = None
    for idx in range(max_steps + 1):
        phi = (pi * idx) / max_steps
        x = base_radius * cos(phi) + phi * base_radius * sin(phi)
        y = base_radius * sin(phi) - phi * base_radius * cos(phi)
        dist = (x * x + y * y) ** 0.5
        theta = atan2(y, x)
        if theta_pitch_intersect is None and dist >= pitch_radius:
            theta_pitch_intersect = theta
            theta_full_tooth = 2.0 * theta_pitch_intersect + theta_tooth
        if theta_pitch_intersect is not None and theta >= theta_full_tooth / 2.0:
            break
        if dist >= outer_radius:
            half_tooth.append((outer_radius * cos(theta), outer_radius * sin(theta)))
        elif dist <= root_radius:
            half_tooth.append((root_radius * cos(theta), root_radius * sin(theta)))
        else:
            half_tooth.append((x, y))

    half_tooth = [rotate_point(point, -theta_full_tooth / 2.0) for point in half_tooth]
    half_tooth_mirror = [flip_y(point) for point in half_tooth[::-1]]
    tooth = half_tooth + half_tooth_mirror

    def arc_points(start, end):
        if start >= end:
            return []
        span = end - start
        count = max(2, int(abs(span) * root_radius / 0.25) + 1)
        return [
            (root_radius * cos(start + span * i / (count - 1)), root_radius * sin(start + span * i / (count - 1)))
            for i in range(count)
        ]

    pitch_half = theta_tooth_and_gap / 2.0
    tooth_min_angle = min(atan2(y, x) for x, y in tooth)
    tooth_max_angle = max(atan2(y, x) for x, y in tooth)
    left_root = arc_points(-pitch_half, tooth_min_angle)
    right_root = arc_points(tooth_max_angle, pitch_half)
    return left_root[:-1] + tooth + right_root[1:]

def gear_points(module, teeth):
    root_r = max(AXLE_R + 0.6, module * teeth / 2.0 - 1.15 * module)
    sector = involute_tooth_and_gap(module, teeth, root_r)
    pitch_angle = 2.0 * pi / teeth
    all_points = []
    for idx in range(teeth):
        rotated = [rotate_point(point, idx * pitch_angle) for point in sector]
        if idx > 0:
            rotated = rotated[1:]
        all_points.extend(rotated)
    return all_points

def make_gear(cx: float, cy: float, teeth: int, phase_deg: float = 0.0):
    with BuildPart() as gear_bp:
        with BuildSketch():
            Polygon(*gear_points(MODULE, teeth), align=Align.NONE)
        extrude(amount=AXLE_Z)
    gear = gear_bp.part
    d_bore = Cylinder(AXLE_R, AXLE_Z + 0.2, align=(Align.CENTER, Align.CENTER, Align.MIN))
    d_bore = d_bore - Box(4.0, 6.0, AXLE_Z + 0.4, align=(Align.MIN, Align.CENTER, Align.MIN)).located(
        Location((BORE_FLAT_X, 0.0, -0.1))
    )
    gear = gear - d_bore
    if abs(phase_deg) > 1e-9:
        gear = gear.rotate(Axis.Z, phase_deg)
    return gear.located(Location((cx, cy, 0.0)))

gear_a = make_gear(0.0, 0.0, teeth=TEETH_A, phase_deg=0.0)
gear_b = make_gear(20.0, 0.0, teeth=TEETH_B, phase_deg=PHASE_B)
gear_c = make_gear(50.0, 0.0, teeth=TEETH_C, phase_deg=PHASE_C)

part = Compound([gear_a, gear_b, gear_c])
