# Cheat Sheet

Section: Core Workflow

Use this when: You need the shortest possible reminder of Build123D syntax and common operations.

Key points:
- Primitives, sketches, booleans, extrude, revolve, loft, sweep.

Source: https://build123d.readthedocs.io/en/latest/cheat_sheet.html

# Cheat Sheet


Stateful Contexts


BuildLine BuildPart BuildSketch


GridLocations HexLocations Locations PolarLocations


Objects


1D - BuildLine


Airfoil


ArcArcTangentArc


ArcArcTangentLine


Bezier


BlendCurve


CenterArc


ConstrainedArcs


ConstrainedLines


DoubleTangentArc


EllipticalCenterArc


ParabolicCenterArc


HyperbolicCenterArc


FilletPolyline


Helix


IntersectingLine


JernArc


Line


PointArcTangentArc


PointArcTangentLine


PolarLine


Polyline


RadiusArc


SagittaArc


Spline


TangentArc


ThreePointArc


2D - BuildSketch


Arrow


ArrowHead


Circle


DimensionLine


Ellipse


ExtensionLine


Polygon


Rectangle


RectangleRounded


RegularPolygon


SlotArc


SlotCenterPoint


SlotCenterToCenter


SlotOverall


Text


TechnicalDrawing


Trapezoid


Triangle


3D - BuildPart


Box


Cone


ConvexPolyhedron


CounterBoreHole


CounterSinkHole


Cylinder


Hole


Sphere


Torus


Wedge


Operations


1D - BuildLine


add()


bounding_box()


mirror()


offset()


project()


scale()


split()


2D - BuildSketch


add()


chamfer()


fillet()


full_round()


make_face()


make_hull()


mirror()


offset()


project()


scale()


split()


sweep()


trace()


3D - BuildPart


add()


chamfer()


draft()


extrude()


fillet()


loft()


make_brake_formed()


mirror()


offset()


project()


revolve()


scale()


section()


split()


sweep()


Selectors


1D - BuildLine


vertices()


edges()


wires()


2D - BuildSketch


vertices()


edges()


wires()


faces()


3D - BuildPart


vertices()


edges()


wires()


faces()


solids()


Selector Operators


Operator


Operand


Method


>


Axis, Edge, Wire, SortBy


sort_by()


<


Axis, Edge, Wire, SortBy


sort_by()


>>


Axis, Edge, Wire, SortBy


group_by()[-1]


<<


Axis, Edge, Wire, SortBy


group_by()[0]


|


Axis, Plane, GeomType


filter_by()


[]


python indexing / slicing


Axis


filter_by_position()


Edge and Wire Operators


Operator


Operand


Method


Description


@


0.0 <= float <= 1.0


position_at()


Position as Vector along object


%


0.0 <= float <= 1.0


tangent_at()


Tangent as Vector along object


^


0.0 <= float <= 1.0


location_at()


Location along object


Shape Operators


Operator


Operand


Method


Description


==


Any


is_same()


Compare CAD objects not including meta data


+


Shape | Iterable[Shape]


Add CAD objects


-


Shape | Iterable[Shape]


Subtract CAD objects


&


Shape | Iterable[Shape]


Intersect CAD objects


Plane Operators


Operator


Operand


Description


==


Plane


Check for equality


!=


Plane


Check for inequality


-


Plane


Reverse direction of normal


*


Location | Shape


Relocate


Location Operators


Operator


Operand


Description


==


Location


Check for equality


!=


Location


Check for inequality


-


Location


Reverse direction of normal


&


Axis | Location | Plane | VectorLike | Shape


Intersect


*


Shape | Location | Iterable[ Location]


Relocate


Vector Operators


Operator


Operand


Method


Description


+


Vector


add()


add


-


Vector


sub()


subtract


*


float


multiply()


multiply by scalar


/


float


multiply()


divide by scalar


Vertex Operators


Operator


Operand


Method


+


Vertex


add()


-


Vertex


sub()


Enums


Align


MIN, CENTER, MAX


ApproxOption


ARC, NONE, SPLINE


AngularDirection


CLOCKWISE, COUNTER_CLOCKWISE


CenterOf


GEOMETRY, MASS, BOUNDING_BOX


Extrinsic


XYZ, XZY, YZX, YXZ, ZXY, ZYX, XYX, XZX, YZY, YXY, ZXZ, ZYZ


FontStyle


REGULAR, BOLD, BOLDITALIC, ITALIC


FrameMethod


CORRECTED, FRENET


GeomType


BEZIER, BSPLINE, CIRCLE, CONE, CYLINDER, ELLIPSE, EXTRUSION, HYPERBOLA, LINE, OFFSET, OTHER, PARABOLA, PLANE, REVOLUTION, SPHERE, TORUS


Intrinsic


XYZ, XZY, YZX, YXZ, ZXY, ZYX, XYX, XZX, YZY, YXY, ZXZ, ZYZ


HeadType


CURVED, FILLETED, STRAIGHT


Keep


ALL, TOP, BOTTOM, BOTH, INSIDE, OUTSIDE


Kind


ARC, INTERSECTION, TANGENT


LengthMode


DIAGONAL, HORIZONTAL, VERTICAL


MeshType


OTHER, MODEL, SUPPORT, SOLIDSUPPORT


Mode


ADD, SUBTRACT, INTERSECT, REPLACE, PRIVATE


NumberDisplay


DECIMAL, FRACTION


PageSize


A0, A1, A2, A3, A4, A5, A6, A7, A8, A9, A10, LEDGER, LEGAL, LETTER


PositionMode


LENGTH, PARAMETER


PrecisionMode


LEAST, AVERAGE, GREATEST, SESSION


Select


ALL, LAST, NEW


Side


BOTH, LEFT, RIGHT


SortBy


LENGTH, RADIUS, AREA, VOLUME, DISTANCE


TextAlign


BOTTOM, CENTER, LEFT, RIGHT, TOP, TOPFIRSTLINE


Transition


RIGHT, ROUND, TRANSFORMED


Unit


MC, MM, CM, M, IN, FT


Until


FIRST, LAST, NEXT, PREVIOUS
