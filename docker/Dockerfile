ARG BASE_IMAGE=ghcr.io/hdl/sim/osvb:latest
FROM ${BASE_IMAGE}

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \ 
      libc++-dev libc++abi-dev \
      clang \
      verilator \
      lcov \
 && rm -rf /var/lib/apt/lists/*

# build with
# docker build -f Dockerfile.covered \
#  --build-arg BASE_IMAGE=ghcr.io/hdl/sim/osvb:latest \
#  -t ghcr.io/you/osvb-with-gcc:0.1 .