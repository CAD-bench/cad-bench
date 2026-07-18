from build123d import *

BOLT_X = -14.0
NUT_X = 14.0
LEAD = 2.70
STARTS = 2
THREAD_H = 7.20
MAJOR_R = 7.30 / 2.0
ROOT_R = 6.00 / 2.0
CLEAR = 0.18
BOLT_HEAD_D = 11.50
BOLT_HEAD_H = 3.60
BOLT_TOTAL_H = 14.20
NUT_OUTER_D = 13.0
NUT_H = 8.0

def male_thread(major_r: float, root_r: float, thread_h: float, lead: float, starts: int):
    thread_depth = major_r - root_r
    path = Helix(pitch=lead, height=thread_h, radius=major_r)
    plane = Plane(origin=path @ 0, z_dir=path % 0, x_dir=(1, 0, 0))
    with BuildSketch(plane) as sk:
        Polygon((0.00, -0.10), (-thread_depth, 0.08), (-thread_depth * 0.9, 0.36), (-0.15, 0.82))
    groove = sweep(sk.sketch.face(), path, is_frenet=True)
    body = Cylinder(major_r, thread_h + lead, align=(Align.CENTER, Align.CENTER, Align.MIN))
    for i in range(starts):
        cutter = groove.rotate(Axis.Z, i * 360.0 / starts).translate((0, 0, i * lead / starts))
        body = body.cut(cutter)
    return body

bolt = male_thread(MAJOR_R, ROOT_R, THREAD_H, LEAD, STARTS)
bolt = bolt.fuse(
    Cylinder(ROOT_R, BOLT_TOTAL_H - THREAD_H, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(
        Location((0, 0, THREAD_H))
    )
)
bolt = bolt.fuse(
    Cylinder(BOLT_HEAD_D / 2.0, BOLT_HEAD_H, align=(Align.CENTER, Align.CENTER, Align.MIN)).located(
        Location((0, 0, BOLT_TOTAL_H))
    )
)

tap = male_thread(MAJOR_R + CLEAR, ROOT_R + CLEAR, THREAD_H, LEAD, STARTS)
nut = Cylinder(NUT_OUTER_D / 2.0, NUT_H, align=(Align.CENTER, Align.CENTER, Align.MIN))
nut = nut.cut(tap.located(Location((0, 0, -0.10))))

part = Compound(
    [
        bolt.located(Location((BOLT_X, 0, 0))),
        nut.located(Location((NUT_X, 0, 0))),
    ]
)
