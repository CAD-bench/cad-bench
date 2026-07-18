Create a transmission part set between two pre-existing axles.

Rig (already present; do not model):
- Input axle center at (0, 0), output axle center at (40, 0), both along +Z
- Axles are D-shafts: nominal diameter 4 mm with flat at x = center_x + 1.5 mm, axle z-range [0, 10]
- Input speed +120 rpm

Your part requirements:
- Produce only transmission geometry in z in [0, 10]
- Target output speed -60 rpm (within +/-10%)
- A direct gear train is expected between the two given axles
- Evaluation uses a rigid-body physical simulation with the fixed D-shafts above
- The geometry must fit those shafts at those exact locations and transfer motion physically

Scoring points:
- direction correctness under physical simulation
- speed/ratio accuracy under physical simulation
- axle fit and preserved placement

Submission contract

Create `/workspace/final.py`. The file must run with Python 3.11 and Build123D 0.10.0, and it must leave the completed model in a top-level variable named `part`. The verifier evaluates that object directly.
