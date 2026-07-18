FROM ghcr.io/cad-bench/cad-bench-agent:harbor-v1

ARG DEBIAN_FRONTEND=noninteractive

ENV CAD_BENCH_TASKS_ROOT=/opt/cad-bench/dataset \
    MPLBACKEND=Agg \
    PYTHONPATH=/opt/cad-bench

RUN uv pip install --system fast-simplification==0.1.13 matplotlib==3.10.8 \
    networkx==3.6.1 numpy==2.4.2 rtree==1.4.1 trimesh==4.11.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends blender ffmpeg libegl1 python3-numpy \
    && rm -rf /var/lib/apt/lists/*

COPY verifier/bench /opt/cad-bench/bench
COPY verifier/tasks /opt/cad-bench/tasks
COPY verifier/simulations /opt/cad-bench/simulations
ARG DATASET_PATH=dataset
COPY ${DATASET_PATH} /opt/cad-bench/dataset
COPY verifier/grader.py verifier/test.sh /tests/
RUN chmod +x /tests/test.sh
