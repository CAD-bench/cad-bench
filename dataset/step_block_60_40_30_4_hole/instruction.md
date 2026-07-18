Create a three-level stepped block.

Requirements:
- Centered at (0, 0) in XY with bottom on z=0 and all geometry in z>=0.
- Base level: 60.0 x 40.0 x 10.0 mm (X,Y,Z).
- Middle level on top of base: 40.0 x 30.0 x 10.0 mm.
- Top level on top of middle: 20.0 x 20.0 x 10.0 mm.
- Total height: 30.0 mm.
- Four through-holes (diameter 6.0 mm) along +Z at:
  - (+15, +10), (+15, -10), (-15, +10), (-15, -10).

Submission contract

Create `/workspace/final.py`. The file must run with Python 3.11 and Build123D 0.10.0, and it must leave the completed model in a top-level variable named `part`. The verifier evaluates that object directly.
