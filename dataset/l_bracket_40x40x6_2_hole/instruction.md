Create an L-bracket plate.

Requirements:
- Plate thickness: 6.0 mm along +Z.
- Bottom face lies on z=0; entire part in z>=0.
- XY footprint is the union of:
  - rectangle [0,40]x[0,20]
  - rectangle [0,20]x[0,40]
- Through-holes (diameter 6.0 mm) along +Z at centers:
  - (10, 10)
  - (30, 10)

Submission contract

Create `/workspace/final.py`. The file must run with Python 3.11 and Build123D 0.10.0, and it must leave the completed model in a top-level variable named `part`. The verifier evaluates that object directly.
