#!/bin/sh

# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -e

cd "$(dirname "$0")/.."

docker build -f Dockerfile-base -t cvdp-agent-base .

MY_AGENT_ROOT="${MY_AGENT_ROOT:-../../../my-agent}"

docker build -f my-agent/Dockerfile-my-agent \
  --build-context my_agent_source="$MY_AGENT_ROOT" \
  -t cvdp-my-agent --no-cache .

docker tag cvdp-my-agent my-agent