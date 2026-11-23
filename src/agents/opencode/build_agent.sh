#!/bin/sh

# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -e

cd "$(dirname "$0")/.."

OPENCODE_CODE_ROOT="${CLAW_CODE_ROOT:-../../../opencode}"

# One base + agent image per cocotb version (v1 -> cocotb 1.9.2, v2 -> 2.0.1),
# so harness-aware runs test with the same cocotb as the verification image.
# v2 is also tagged :latest for callers that don't pick a version.
# Build a single version with: COCOTB_VERSIONS=v2 sh build_agent.sh
for V in ${COCOTB_VERSIONS:-v1 v2}; do
	LATEST_BASE=""; LATEST_AGENT=""
	if [ "$V" = v2 ]; then
		LATEST_BASE="-t cvdp-agent-base:latest"
		LATEST_AGENT="-t cvdp-opencode-agent:latest"
	fi

	docker build -f Dockerfile-base --build-arg COCOTB_VERSION="$V" \
		-t cvdp-agent-base:"$V" $LATEST_BASE .

	docker build -f opencode/Dockerfile-agent \
		--build-arg BASE_IMAGE=cvdp-agent-base:"$V" \
		--build-context opencode_source="$OPENCODE_CODE_ROOT" \
		-t cvdp-opencode-agent:"$V" $LATEST_AGENT --no-cache .

	docker tag cvdp-opencode-agent:"$V" opencode-agent:"$V"
	if [ "$V" = v2 ]; then
		docker tag cvdp-opencode-agent:v2 opencode-agent:latest
	fi
done
