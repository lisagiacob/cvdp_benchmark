#!/bin/sh

set -e

cd "$(dirname "$0")/.."

# One base + agent image per cocotb version (v1 -> cocotb 1.9.2, v2 -> 2.0.1),
# so harness-aware runs test with the same cocotb as the verification image.
# v2 is also tagged :latest for callers that don't pick a version.
# Build a single version with: COCOTB_VERSIONS=v2 sh build_agent.sh
for V in ${COCOTB_VERSIONS:-v1 v2}; do
	LATEST_BASE=""; LATEST_AGENT=""
	if [ "$V" = v2 ]; then
		LATEST_BASE="-t cvdp-agent-base:latest"
		LATEST_AGENT="-t cvdp-claude-code-agent:latest"
	fi

	docker build -f Dockerfile-base --build-arg COCOTB_VERSION="$V" \
		-t cvdp-agent-base:"$V" $LATEST_BASE .

	docker build -f ClaudeCode/Dockerfile-claude-code-agent \
		--build-arg BASE_IMAGE=cvdp-agent-base:"$V" \
		-t cvdp-claude-code-agent:"$V" $LATEST_AGENT --no-cache ClaudeCode/

	docker tag cvdp-claude-code-agent:"$V" claude-code-agent:"$V"
	if [ "$V" = v2 ]; then
		docker tag cvdp-claude-code-agent:v2 claude-code-agent:latest
	fi
done
