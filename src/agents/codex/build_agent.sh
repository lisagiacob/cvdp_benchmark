#!/bin/sh

set -e

cd "$(dirname "$0")/.."

# Pin the Codex CLI to an exact release so the image layer is cached until a
# new version ships (no --no-cache needed). Override with CODEX_VERSION=x.y.z.
if [ -z "$CODEX_VERSION" ]; then
	CODEX_VERSION="$(npm view @openai/codex version 2>/dev/null)"
fi
if [ -z "$CODEX_VERSION" ]; then
	echo "Could not resolve the latest @openai/codex version (is npm installed?)." >&2
	echo "Pass it explicitly: CODEX_VERSION=x.y.z sh build_agent.sh" >&2
	exit 1
fi
echo "Building Codex agent with @openai/codex@$CODEX_VERSION"

# One base + agent image per cocotb version (v1 -> cocotb 1.9.2, v2 -> 2.0.1),
# so harness-aware runs test with the same cocotb as the verification image.
# v2 is also tagged :latest for callers that don't pick a version.
# Build a single version with: COCOTB_VERSIONS=v2 sh build_agent.sh
for V in ${COCOTB_VERSIONS:-v1 v2}; do
	LATEST_BASE=""; LATEST_AGENT=""
	if [ "$V" = v2 ]; then
		LATEST_BASE="-t cvdp-agent-base:latest"
		LATEST_AGENT="-t cvdp-codex-agent:latest"
	fi

	docker build -f Dockerfile-base --build-arg COCOTB_VERSION="$V" \
		-t cvdp-agent-base:"$V" $LATEST_BASE .

	docker build -f codex/Dockerfile-agent \
		--build-arg BASE_IMAGE=cvdp-agent-base:"$V" \
		--build-arg CODEX_VERSION="$CODEX_VERSION" \
		-t cvdp-codex-agent:"$V" -t cvdp-codex-agent:"$V"-"$CODEX_VERSION" \
		$LATEST_AGENT codex/

	docker tag cvdp-codex-agent:"$V" codex-agent:"$V"
	if [ "$V" = v2 ]; then
		docker tag cvdp-codex-agent:v2 codex-agent:latest
	fi
done
