from build123d import *

AXLE_Z = 10.0
AXLE_DIST = 40.0

# Small gap keeps the two gears as separate components after tessellation/export.
GEAR_GAP = 0.03
TARGET_RATIO_MAG = 0.5
R_B = (AXLE_DIST - GEAR_GAP) / (1.0 + TARGET_RATIO_MAG)
R_A = (AXLE_DIST - GEAR_GAP) - R_B
AXLE_R = 2.0
BORE_FLAT_X = 1.5
TOOTH_OVERLAP = 0.25

def make_gear(cx: float, cy: float, outer_r: float, teeth: int, tooth_h: float, tooth_w: float):
    core = Cylinder(outer_r - tooth_h + TOOTH_OVERLAP, AXLE_Z, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(
        Location((cx, cy, 0.0))
    )
    with BuildPart() as tooth_bp:
        with PolarLocations(outer_r - tooth_h / 2.0, teeth):
            Box(tooth_h, tooth_w, AXLE_Z, align=(Align.CENTER, Align.CENTER, Align.MIN))
    teeth_part = tooth_bp.part.located(Location((cx, cy, 0.0)))
    gear = core + teeth_part
    d_bore = Cylinder(AXLE_R, AXLE_Z + 0.2, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(Location((cx, cy, 0.0)))
    d_bore = d_bore - Box(4.0, 6.0, AXLE_Z + 0.4, align=(Align.MIN, Align.CENTER, Align.MIN)).located(
        Location((cx + BORE_FLAT_X, cy, -0.1))
    )
    gear = gear - d_bore
    return gear

TEETH_A = 23
TEETH_B = 50
TOOTH_H = 1.2
TOOTH_W_A = 2.1
TOOTH_W_B = 2.1

gear_a = make_gear(0.0, 0.0, R_A, teeth=TEETH_A, tooth_h=TOOTH_H, tooth_w=TOOTH_W_A)
gear_b = make_gear(40.0, 0.0, R_B, teeth=TEETH_B, tooth_h=TOOTH_H, tooth_w=TOOTH_W_B)
part = Compound([gear_a, gear_b])
