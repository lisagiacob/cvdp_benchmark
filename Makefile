RUN := python3

# Refuses to start if the target's BENCHMARK_PREFIX dir is non-empty, then
# runs $(1) (a run_benchmark.py invocation), emails a notification with
# the outcome via tools/notify_email.py, and finally re-raises $(1)'s exit
# status so the make target still fails correctly on error. Email is skipped
# silently if NOTIFY_EMAIL_TO isn't set in .env.
define run_and_notify
	@if [ -d "$$BENCHMARK_PREFIX" ] && [ -n "$$(ls -A "$$BENCHMARK_PREFIX")" ]; then \
		echo "Error: $$BENCHMARK_PREFIX/ exists and is not empty. Remove it manually before proceeding." >&2; \
		exit 1; \
	fi
	@START=$$(date +%s); \
	$(1) --network-name "cvdp-bridge-$$BENCHMARK_PREFIX"; STATUS=$$?; \
	END=$$(date +%s); \
	$(RUN) tools/notify_email.py --target "$(2)" --status $$STATUS --duration $$((END - START)) --prefix "$$BENCHMARK_PREFIX"; \
	exit $$STATUS
endef

# Build of the main container for verification with the 2 versions. Each
# version gets its own tag so both can coexist; the benchmark targets below
# pick the matching one through OSS_SIM_IMAGE (overriding the .env value).
.PHONY: cocov1 cocov2
cocov1:
	docker build -f docker/Dockerfile.osvb-cocotb -t osvb-cocotb:v1 . --build-arg COCOTB_VERSION=v1

cocov2:
	docker build -f docker/Dockerfile.osvb-cocotb -t osvb-cocotb:v2 . --build-arg COCOTB_VERSION=v2

# osvb-cocotb:v2 + the packages some heavy harnesses need (for the *-heavy targets)
.PHONY: cocov2-heavy
cocov2-heavy:
	docker build -f docker/Dockerfile.heavy-extras -t osvb-cocotb:v2-heavy . \
		--build-arg BASE=osvb-cocotb:v2 --build-arg PKGS="aes==1.2.0 cocotbext-axi==0.1.28"

COCOV1_TARGETS := nonagentic-codegen-v1 opencode-cocov1 claude-cocov1 codex-cocov1 \
	opencode-cocov1-ba claude-cocov1-ba codex-cocov1-ba
COCOV2_TARGETS := nonagentic-codegen-v2 opencode-cocov2 claude-cocov2 codex-cocov2 \
	opencode-cocov2-ba claude-cocov2-ba codex-cocov2-ba \
	opencode-sd claude-sd codex-sd
# CVDP v1.1.0: every harness migrated to cocotb 2 (a single no_commercial file,
# no V1/V2 split), so all targets use the v2 images and NEW_HARNESS (see .env.example)
V110_NOCOMM := dataset/cvdp_v1.1.0_agentic_code_generation_no_commercial.jsonl
V110_COMM := dataset/cvdp_v1.1.0_agentic_code_generation_commercial.jsonl
V110_NA_NOCOMM := dataset/cvdp_v1.1.0_nonagentic_code_generation_no_commercial.jsonl
V110_NA_COMM := dataset/cvdp_v1.1.0_nonagentic_code_generation_commercial.jsonl
V110_TARGETS := opencode-v110 claude-v110 codex-v110 \
	opencode-v110-nonagentic opencode-v110-nonagentic-commercial \
	opencode-v110-nonagentic-toconv opencode-v110-nonagentic-commercial-toconv \
	opencode-v110-ba claude-v110-ba codex-v110-ba \
	opencode-v110-commercial claude-v110-commercial codex-v110-commercial \
	opencode-v110-commercial-eda claude-v110-commercial-eda codex-v110-commercial-eda \
	opencode-v110-commercial-ba claude-v110-commercial-ba codex-v110-commercial-ba \
	opencode-v110-ba-toconv opencode-v110-commercial-ba-toconv \
	opencode-v110-bu-toconv opencode-v110-commercial-bu-toconv \
	opencode-v110-nonagentic-bu-toconv opencode-v110-nonagentic-commercial-bu-toconv
# CVDP v1.1.0 context-heavy agentic: each datapoint is a task on a real git repo,
# checked out from the HF bundles in HEAVY_REPOS (CVDP_HEAVY_REPOS_PATH). Agentic
# only, cocotb 2 like the other v1.1.0 targets. Override the bundle dir with
# HEAVY_REPOS=<dir> on the command line.
HEAVY_NOCOMM := dataset/cvdp_v1.1.0_agentic_heavy_code_generation_no_commercial.jsonl
HEAVY_COMM := dataset/cvdp_v1.1.0_agentic_heavy_code_generation_commercial.jsonl
HEAVY_REPOS ?= $(abspath $(CURDIR)/../cvdp_v1.1.0_agentic_heavy_code_generation_public)
HEAVY_TARGETS := opencode-heavy claude-heavy codex-heavy \
	opencode-heavy-commercial claude-heavy-commercial codex-heavy-commercial \
	opencode-heavy-ba claude-heavy-ba codex-heavy-ba \
	opencode-heavy-commercial-ba claude-heavy-commercial-ba codex-heavy-commercial-ba
# Same settings as the v1.1.0 targets, but with the *-heavy images (base image +
# the extra Python packages some heavy harnesses import, see
# docker/Dockerfile.heavy-extras), so osvb-cocotb:v2 and cadence :cocotb2 stay
# identical to the ones used by the other runs.
$(HEAVY_TARGETS): export OSS_SIM_IMAGE = osvb-cocotb:v2-heavy
$(HEAVY_TARGETS): export VERIF_EDA_IMAGE = cvdp-cadence-verif:cocotb2-heavy
$(HEAVY_TARGETS): export NEW_HARNESS = true
$(HEAVY_TARGETS): export CVDP_HEAVY_REPOS_PATH = $(HEAVY_REPOS)
BENCH_TARGETS := $(COCOV1_TARGETS) $(COCOV2_TARGETS) $(V110_TARGETS) $(HEAVY_TARGETS) \
	opencode-commercial claude-commercial codex-commercial \
	opencode-commercial-eda claude-commercial-eda codex-commercial-eda \
	opencode-commercial-ba claude-commercial-ba codex-commercial-ba

# The OpenCode / Claude Code / Codex targets also pass the agent image built for the
# same cocotb version (-g <agent>:v1|v2, see src/agents/*/build_agent.sh).
$(COCOV1_TARGETS): export OSS_SIM_IMAGE = osvb-cocotb:v1
$(COCOV2_TARGETS): export OSS_SIM_IMAGE = osvb-cocotb:v2
$(V110_TARGETS): export OSS_SIM_IMAGE = osvb-cocotb:v2
$(V110_TARGETS): export VERIF_EDA_IMAGE = cvdp-cadence-verif:cocotb2
$(V110_TARGETS): export NEW_HARNESS = true

# Each benchmark target writes to its own work_<target>/ dir, so different
# targets can run in parallel (e.g. in separate tmux sessions). Override with
# BENCHMARK_PREFIX=<dir> on the command line.
$(BENCH_TARGETS): export BENCHMARK_PREFIX ?= work_$@

# Benchmarks — non-agentic
.PHONY: nonagentic-codegen-v1 nonagentic-codegen-v2

nonagentic-codegen-v1:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_nonagentic_code_generation_no_commercial_COCOTBV1.jsonl -l -m $(or $(MODEL),azure-gpt-4o),nonagentic-codegen-v1)

nonagentic-codegen-v2:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_nonagentic_code_generation_no_commercial_COCOTBV2.jsonl -l -m $(or $(MODEL),azure-gpt-4o),nonagentic-codegen-v2)

# ------------------------------------ OPENCODE ------------------------------------
.PHONY: opencode-up
opencode-up:
	bash src/agents/opencode/build_agent.sh

# Benchmarks — agentic (require opencode container running)
.PHONY: opencode-cocov1 opencode-cocov2 opencode-sd opencode-commercial opencode-commercial-eda

opencode-cocov1:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV1.jsonl -l -g cvdp-opencode-agent:v1,opencode-cocov1)

opencode-cocov2:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl -l -g cvdp-opencode-agent:v2,opencode-cocov2)

opencode-sd:
# It is possible to specify the dp adding it as an argument DP=
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl -l -g cvdp-opencode-agent:v2 -i $(or $(DP),cvdp_agentic_AES_encryption_decryption_0018),opencode-sd)

opencode-commercial:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_commercial.jsonl -l -g cvdp-opencode-agent,opencode-commercial-vers)

# Same as opencode-commercial, with Cadence xrun available inside the agent container
opencode-commercial-eda:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_commercial.jsonl -l -g cvdp-opencode-agent --agent-include-eda,opencode-commercial-eda)

# Harness aware (BA): the harness tests are mounted in the agent container at /code/src
.PHONY: opencode-cocov1-ba opencode-cocov2-ba opencode-commercial-ba
opencode-cocov1-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV1.jsonl -l -g cvdp-opencode-agent:v1 --force-agentic-include-harness,opencode-cocov1-ba)

opencode-cocov2-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl -l -g cvdp-opencode-agent:v2 --force-agentic-include-harness,opencode-cocov2-ba)

# Commercial harness-aware runs always get Cadence xrun, so the agent can run the
# official tests (which call xrun) with the grading simulator
opencode-commercial-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_commercial.jsonl -l -g cvdp-opencode-agent --force-agentic-include-harness --agent-include-eda,opencode-commercial-ba)


opencode-v110:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NOCOMM) -l -g cvdp-opencode-agent:v2,opencode-v110)

opencode-v110-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NOCOMM) -l -g cvdp-opencode-agent:v2 --force-agentic-include-harness,opencode-v110-ba)

opencode-v110-commercial:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_COMM) -l -g cvdp-opencode-agent:v2,opencode-v110-commercial)

opencode-v110-commercial-eda:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_COMM) -l -g cvdp-opencode-agent:v2 --agent-include-eda,opencode-v110-commercial-eda)

opencode-v110-commercial-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_COMM) -l -g cvdp-opencode-agent:v2 --force-agentic-include-harness --agent-include-eda,opencode-v110-commercial-ba)

# Iterate to convergence (BA): after each agent session the official harness runs
# and, while it fails, the session is resumed with its output (ITC_* in .env).
# Single thread on purpose, so agent times stay comparable with the other runs.
.PHONY: opencode-v110-ba-toconv opencode-v110-commercial-ba-toconv
opencode-v110-ba-toconv:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NOCOMM) -l -g cvdp-opencode-agent:v2 --force-agentic-include-harness --iter-to-convergence -t 1,opencode-v110-ba-toconv)

opencode-v110-commercial-ba-toconv:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_COMM) -l -g cvdp-opencode-agent:v2 --force-agentic-include-harness --agent-include-eda --iter-to-convergence -t 1,opencode-v110-commercial-ba-toconv)

# Force-agentic: the v1.1.0 non-agentic (copilot) datasets run through the
# agentic pipeline with OpenCode (--force-agentic converts each datapoint)
.PHONY: opencode-v110-nonagentic opencode-v110-nonagentic-commercial
opencode-v110-nonagentic:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NA_NOCOMM) -l -g cvdp-opencode-agent:v2 --force-agentic,opencode-v110-nonagentic)

opencode-v110-nonagentic-commercial:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NA_COMM) -l -g cvdp-opencode-agent:v2 --force-agentic --agent-include-eda,opencode-v110-nonagentic-commercial)

# Non-agentic datasets iterated to convergence (BA, like the agentic *-toconv targets).
# Single thread on purpose, so agent times stay comparable with the other runs.
.PHONY: opencode-v110-nonagentic-toconv opencode-v110-nonagentic-commercial-toconv
opencode-v110-nonagentic-toconv:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NA_NOCOMM) -l -g cvdp-opencode-agent:v2 --force-agentic --force-agentic-include-harness --iter-to-convergence -t 1,opencode-v110-nonagentic-toconv-final)

opencode-v110-nonagentic-commercial-toconv:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NA_COMM) -l -g cvdp-opencode-agent:v2 --force-agentic --force-agentic-include-harness --agent-include-eda --iter-to-convergence -t 1,opencode-v110-nonagentic-commercial-toconv-final)

# Iterate to convergence, harness unaware (HUF): src/ is not mounted
# in the agent container, the agent only gets the failure feedback, and rundir/ is
# cleared of the official harness artifacts before each new session.
# Single thread on purpose, so agent times stay comparable with the other runs.
.PHONY: opencode-v110-bu-toconv opencode-v110-commercial-bu-toconv \
	opencode-v110-nonagentic-bu-toconv opencode-v110-nonagentic-commercial-bu-toconv
opencode-v110-bu-toconv:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NOCOMM) -l -g cvdp-opencode-agent:v2 --iter-to-convergence -t 1,opencode-v110-bu-toconv)

opencode-v110-commercial-bu-toconv:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_COMM) -l -g cvdp-opencode-agent:v2 --agent-include-eda --iter-to-convergence -t 1,opencode-v110-commercial-bu-toconv)

opencode-v110-nonagentic-bu-toconv:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NA_NOCOMM) -l -g cvdp-opencode-agent:v2 --force-agentic --iter-to-convergence -t 1,opencode-v110-nonagentic-bu-toconv)

opencode-v110-nonagentic-commercial-bu-toconv:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NA_COMM) -l -g cvdp-opencode-agent:v2 --force-agentic --agent-include-eda --iter-to-convergence -t 1,opencode-v110-nonagentic-commercial-bu-toconv)


# ----------------------------------- CLAUDE CODE ------------------------------------
.PHONY: claude-up
claude-up:
	bash src/agents/ClaudeCode/build_agent.sh

# Cadence EDA image (required for commercial benchmarks, e.g. claude-commercial)
.PHONY: cadence-up cadence-cocotb2
cadence-up:
	docker build -t cvdp-cadence-verif:latest -f docker/Dockerfile-cadence-verif .

# Same image with cocotb 2.0.1, for the v1.1.0 commercial dataset (needs cadence-up first)
cadence-cocotb2:
	docker build -t cvdp-cadence-verif:cocotb2 -f docker/Dockerfile-cadence-verif-cocotb2 .

# cvdp-cadence-verif:cocotb2 + aes, for the *-heavy-commercial targets (needs cadence-cocotb2 first)
.PHONY: cadence-cocotb2-heavy
cadence-cocotb2-heavy:
	docker build -f docker/Dockerfile.heavy-extras -t cvdp-cadence-verif:cocotb2-heavy . \
		--build-arg BASE=cvdp-cadence-verif:cocotb2 --build-arg PKGS="aes==1.2.0"

# Benchmarks — agentic (require claude code container running)
.PHONY: claude-cocov1 claude-cocov2 claude-sd claude-commercial claude-commercial-eda
claude-cocov1:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV1.jsonl -l -g claude-code-agent:v1,claude-cocov1)

claude-cocov2:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl -l -g claude-code-agent:v2,claude-cocov2)

claude-sd:
# It is possible to specify the dp adding it as an argument DP=
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl -l -g claude-code-agent:v2 -i $(or $(DP),cvdp_agentic_AES_encryption_decryption_0018),claude-sd)

claude-commercial:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_commercial.jsonl -l -g claude-code-agent,claude-commercial)

# Same as claude-commercial, with Cadence xrun available inside the agent container
claude-commercial-eda:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_commercial.jsonl -l -g claude-code-agent --agent-include-eda,claude-commercial-eda)

# Harness aware (BA): the harness tests are mounted in the agent container at /code/src
.PHONY: claude-cocov1-ba claude-cocov2-ba claude-commercial-ba
claude-cocov1-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV1.jsonl -l -g claude-code-agent:v1 --force-agentic-include-harness,claude-cocov1-ba)

claude-cocov2-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl -l -g claude-code-agent:v2 --force-agentic-include-harness,claude-cocov2-ba)

# Commercial harness-aware runs always get Cadence xrun, so the agent can run the
# official tests (which call xrun) with the grading simulator
claude-commercial-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_commercial.jsonl -l -g claude-code-agent --force-agentic-include-harness --agent-include-eda,claude-commercial-ba)


# -------------------------------------- CODEX --------------------------------------
# Rebuilds only the CLI layer when a new @openai/codex release is out
# (pin one with CODEX_VERSION=x.y.z).
.PHONY: codex-up
codex-up:
	bash src/agents/codex/build_agent.sh

# Benchmarks — agentic (require codex container built)
.PHONY: codex-cocov1 codex-cocov2 codex-sd codex-commercial codex-commercial-eda
codex-cocov1:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV1.jsonl -l -g codex-agent:v1,codex-cocov1)

codex-cocov2:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl -l -g codex-agent:v2,codex-cocov2)

codex-sd:
# It is possible to specify the dp adding it as an argument DP=
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl -l -g codex-agent:v2 -i $(or $(DP),cvdp_agentic_AES_encryption_decryption_0018),codex-sd)

codex-commercial:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_commercial.jsonl -l -g codex-agent,codex-commercial)

# Same as codex-commercial, with Cadence xrun available inside the agent container
codex-commercial-eda:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_commercial.jsonl -l -g codex-agent --agent-include-eda,codex-commercial-eda)

# Harness aware (BA): the harness tests are mounted in the agent container at /code/src
.PHONY: codex-cocov1-ba codex-cocov2-ba codex-commercial-ba
codex-cocov1-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV1.jsonl -l -g codex-agent:v1 --force-agentic-include-harness,codex-cocov1-ba)

codex-cocov2-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl -l -g codex-agent:v2 --force-agentic-include-harness,codex-cocov2-ba)

# Commercial harness-aware runs always get Cadence xrun, so the agent can run the
# official tests (which call xrun) with the grading simulator
codex-commercial-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_commercial.jsonl -l -g codex-agent --force-agentic-include-harness --agent-include-eda,codex-commercial-ba)



# CLAUDE 

claude-v110:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NOCOMM) -l -g claude-code-agent:v2,claude-v110)

claude-v110-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NOCOMM) -l -g claude-code-agent:v2 --force-agentic-include-harness,claude-v110-ba)

claude-v110-commercial:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_COMM) -l -g claude-code-agent:v2,claude-v110-commercial)

claude-v110-commercial-eda:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_COMM) -l -g claude-code-agent:v2 --agent-include-eda,claude-v110-commercial-eda)

claude-v110-commercial-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_COMM) -l -g claude-code-agent:v2 --force-agentic-include-harness --agent-include-eda,claude-v110-commercial-ba)

codex-v110:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NOCOMM) -l -g codex-agent:v2,codex-v110)

codex-v110-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_NOCOMM) -l -g codex-agent:v2 --force-agentic-include-harness,codex-v110-ba)

codex-v110-commercial:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_COMM) -l -g codex-agent:v2,codex-v110-commercial)

codex-v110-commercial-eda:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_COMM) -l -g codex-agent:v2 --agent-include-eda,codex-v110-commercial-eda)

codex-v110-commercial-ba:
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(V110_COMM) -l -g codex-agent:v2 --force-agentic-include-harness --agent-include-eda,codex-v110-commercial-ba)

# ------------------------------ HEAVY AGENTIC (v1.1.0) ------------------------------
# Needs the bundles from HF in HEAVY_REPOS (default ../cvdp_v1.1.0_agentic_heavy_code_generation_public)
# and the images from `make cocov2-heavy` (+ `make cadence-cocotb2-heavy` for *-commercial).
# One datapoint only: DP=<id>, e.g. make claude-heavy DP=cvdp_agentic_heavy_rvx_0004
.PHONY: $(HEAVY_TARGETS) heavy-check
HEAVY_DP = $(if $(DP),-i $(DP))

heavy-check:
	@if [ ! -d "$(HEAVY_REPOS)" ] || [ -z "$$(ls "$(HEAVY_REPOS)"/*.bundle 2>/dev/null)" ]; then \
		echo "Error: no .bundle files in $(HEAVY_REPOS). Download them from HF or pass HEAVY_REPOS=<dir>." >&2; exit 1; \
	fi

opencode-heavy: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_NOCOMM) -l -g cvdp-opencode-agent:v2 $(HEAVY_DP),opencode-heavy)

claude-heavy: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_NOCOMM) -l -g claude-code-agent:v2 $(HEAVY_DP),claude-heavy)

codex-heavy: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_NOCOMM) -l -g codex-agent:v2 $(HEAVY_DP),codex-heavy)

opencode-heavy-commercial: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_COMM) -l -g cvdp-opencode-agent:v2 $(HEAVY_DP),opencode-heavy-commercial)

claude-heavy-commercial: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_COMM) -l -g claude-code-agent:v2 $(HEAVY_DP),claude-heavy-commercial)

codex-heavy-commercial: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_COMM) -l -g codex-agent:v2 $(HEAVY_DP),codex-heavy-commercial)

# Harness aware (HAF/BA): the harness is mounted read-only in the agent container.
# For heavy datapoints it goes to /harness/src + /harness/docker-compose.yml (not
# /code/src: some repos have their own src/) and the prompt says where it is.
# Commercial BA also gets Cadence xrun, like the other *-commercial-ba targets.
opencode-heavy-ba: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_NOCOMM) -l -g cvdp-opencode-agent:v2 --force-agentic-include-harness $(HEAVY_DP),opencode-heavy-ba)

claude-heavy-ba: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_NOCOMM) -l -g claude-code-agent:v2 --force-agentic-include-harness $(HEAVY_DP),claude-heavy-ba)

codex-heavy-ba: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_NOCOMM) -l -g codex-agent:v2 --force-agentic-include-harness $(HEAVY_DP),codex-heavy-ba)

opencode-heavy-commercial-ba: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_COMM) -l -g cvdp-opencode-agent:v2 --force-agentic-include-harness --agent-include-eda $(HEAVY_DP),opencode-heavy-commercial-ba)

claude-heavy-commercial-ba: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_COMM) -l -g claude-code-agent:v2 --force-agentic-include-harness --agent-include-eda $(HEAVY_DP),claude-heavy-commercial-ba)

codex-heavy-commercial-ba: heavy-check
	$(call run_and_notify,$(RUN) run_benchmark.py -f $(HEAVY_COMM) -l -g codex-agent:v2 --force-agentic-include-harness --agent-include-eda $(HEAVY_DP),codex-heavy-commercial-ba)

# ----------------------------- HARNESS RERUN (v1.1.0) ------------------------------
# Re-grades the agent output of a finished v1.0.2 run with the v1.1.0 harness,
# without re-running the agent (the agent inputs are identical across the two
# versions). Works on a copy: WORK=work_x is left untouched, results go to
# work_x_rr/ (report regenerated at the end), the log to work_x_rr.log.
#   make rerun-harness WORK=work_opencode_cocov2-ba_5-5
# Optional: DRY=1 (only print what would run), RR_OUT=<dir> (default <WORK>_rr),
# AGENT=<image> (default: read from the run), DATASET=commercial|no_commercial
# (default: commercial if WORK's name contains "commercial").
RR_OUT ?= $(WORK)_rr
RR_KIND = $(or $(DATASET),$(if $(findstring commercial,$(WORK)),commercial,no_commercial))
RR_DATASET = dataset/cvdp_v1.1.0_agentic_code_generation_$(RR_KIND).jsonl
RR_EDA_ARGS = $(if $(filter commercial,$(RR_KIND)),--new-harness --verif-eda-image cvdp-cadence-verif:cocotb2)

.PHONY: rerun-harness
rerun-harness: SHELL := /bin/bash
rerun-harness:
	@if [ -z "$(WORK)" ] || [ ! -d "$(WORK)" ]; then \
		echo "Usage: make rerun-harness WORK=<existing work_* dir> [DRY=1]" >&2; exit 1; \
	fi
	@if [ -e "$(RR_OUT)" ] && [ -z "$(DRY)" ]; then \
		echo "Error: $(RR_OUT) already exists. Move or delete it first (or pass RR_OUT=<dir>)." >&2; exit 1; \
	fi
	@AGENT_IMG="$(AGENT)"; \
	[ -n "$$AGENT_IMG" ] || AGENT_IMG=$$(grep -h -m1 'image:' $(WORK)/*/harness/*/docker-compose-agent.yml 2>/dev/null | head -1 | awk '{print $$2}'); \
	[ -n "$$AGENT_IMG" ] || { echo "Error: agent image not found in $(WORK), pass AGENT=<image>" >&2; exit 1; }; \
	echo "Rerun $(WORK) -> $(RR_OUT) ($(RR_KIND), agent $$AGENT_IMG)"; \
	$(RUN) -u tools/resume_agentic_eval.py --dataset $(RR_DATASET) \
		--prefix $(WORK) --copy-to $(RR_OUT) --agent "$$AGENT_IMG" \
		--only-eval --refresh-harness --oss-sim-image osvb-cocotb:v2 $(RR_EDA_ARGS) \
		$(if $(DRY),--dry-run) 2>&1 | tee $(if $(DRY),/dev/null,$(RR_OUT).log); \
	STATUS=$${PIPESTATUS[0]}; [ $$STATUS -eq 0 ] || exit $$STATUS; \
	[ -n "$(DRY)" ] || NEW_HARNESS=true $(RUN) run_benchmark.py -f $(RR_DATASET) -l -g "$$AGENT_IMG" \
		-p $(RR_OUT) --regenerate-report

# Utilities
.PHONY: clean setup change-model

# Set LLM_MODEL in .env (the model every agent runs), e.g.
#   make change-model MODEL=bedrock/global.anthropic.claude-sonnet-4-6
change-model:
	@if [ -z "$(MODEL)" ]; then \
		echo "Usage: make change-model MODEL=<provider>/<model>" >&2; \
		exit 1; \
	fi
	@touch .env
	@if grep -q '^LLM_MODEL=' .env; then \
		sed -i 's|^LLM_MODEL=.*|LLM_MODEL=$(MODEL)|' .env; \
	else \
		echo 'LLM_MODEL=$(MODEL)' >> .env; \
	fi
	@echo "LLM_MODEL=$(MODEL)"
clean:
	@if [ -d work ] && [ -n "$$(ls -A work)" ]; then \
		echo "Error: work/ exists and is not empty. Remove it manually before proceeding." >&2; \
		exit 1; \
	fi

setup:
	@if [ -f .env ]; then \
		echo "⚠  .env already exists. Remove it first if you want a fresh setup."; \
		exit 1; \
	fi
	@cp .env.example .env
	@echo "Created .env from .env.example."
	@echo ""
	@echo "Configure at minimum:"
	@echo "  - LLM_MODEL (e.g. bedrock/global.anthropic.claude-sonnet-4-6), or: make change-model MODEL=..."
	@echo "  - LLM credentials (AWS keys for Bedrock, or API key + base URL for others)"
	@echo ""
	@echo "Optional tuning:"
	@echo "  - DOCKER_TIMEOUT_AGENT (default 600s)"
	@echo ""
	@echo "Edit .env now."
