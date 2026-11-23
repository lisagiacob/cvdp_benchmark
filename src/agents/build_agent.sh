#!/bin/sh

# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

cd "$(dirname "$0")"

docker build -f Dockerfile-base -t cvdp-agent-base .

#docker build -f Dockerfile-agent -t cvdp-opencode-agent --no-cache .
#docker tag cvdp-opencode-agent opencode-agent

#docker build -f Dockerfile-my-agent -t cvdp-my-agent --no-cache .
#docker tag cvdp-my-agent my-agent
