Create a heavily-featured mounting plate.

Requirements:
- Plate size: 100.0 mm (X) x 60.0 mm (Y) x 12.0 mm (Z).
- Centered at (0, 0) in XY.
- Bottom face on z=0 and entire part in z>=0.
- One centered through-slot: 50.0 mm long (X direction), 12.0 mm wide (Y), through all thickness.
- Eight through-holes (diameter 6.0 mm) at:
  - (±40, ±20) and (±20, ±20).

Submission contract

Create `/workspace/final.py`. The file must run with Python 3.11 and Build123D 0.10.0, and it must leave the completed model in a top-level variable named `part`. The verifier evaluates that object directly.
