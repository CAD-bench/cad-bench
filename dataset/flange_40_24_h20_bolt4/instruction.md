Create a two-step flange spacer.

Requirements:
- Axis is global Z.
- Part sits in z>=0 with base on z=0.
- Base disk: diameter 40.0 mm, thickness 8.0 mm.
- Top boss: diameter 24.0 mm, height 12.0 mm (on top of base).
- Total height: 20.0 mm.
- One center through-bore: diameter 12.0 mm through full height.
- Four through bolt holes: diameter 5.0 mm on 30.0 mm bolt circle
  at angles 0, 90, 180, 270 degrees.

Submission contract

Create `/workspace/final.py`. The file must run with Python 3.11 and Build123D 0.10.0, and it must leave the completed model in a top-level variable named `part`. The verifier evaluates that object directly.
