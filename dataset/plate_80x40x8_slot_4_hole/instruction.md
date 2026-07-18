Create a slotted mounting plate.

Requirements:
- Plate size: 80.0 mm (X) x 40.0 mm (Y) x 8.0 mm (Z).
- Bottom face on z=0 and entire part in z>=0.
- Centered at (0, 0) in XY.
- One centered through slot: 40.0 mm long (X direction), 10.0 mm wide (Y), through all thickness.
- Four through-holes (diameter 6.0 mm) at XY:
  - (+30, +15), (+30, -15), (-30, +15), (-30, -15).

Submission contract

Create `/workspace/final.py`. The file must run with Python 3.11 and Build123D 0.10.0, and it must leave the completed model in a top-level variable named `part`. The verifier evaluates that object directly.
