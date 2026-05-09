from build123d import *

part = import_step("__TASK_DIR__/fixtures/mcmaster/91290A111.step")
part = part.located(Location((0, 0, -part.bounding_box().max.Z)))
