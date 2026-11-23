#!/usr/bin/env python3
"""
Resume an interrupted agentic run without re-running the LLM agent on
datapoints that already completed.

Why this exists: run_benchmark.py has no resume flag. --id always redoes
prepare+agent+eval from scratch for that id, and calling repo.prepare()
again on an already-agent-processed id would silently overwrite its
rtl/verif/docs files with the pre-agent context (agent_results only lives
in-memory, in the process that ran the agent).

Split logic:
  - id has harness/<issue>/agent_changes.patch  -> agent already completed
    (th_agent only writes this file after agent_run() returns and the
    before/after diff is computed). Evaluate it directly against the
    existing on-disk state, WITHOUT touching prepare()/Repository().
  - id has no agent_changes.patch               -> agent never finished
    (or was the one in flight when the session died). Nothing worth
    preserving; just re-run it normally through run_benchmark.py --id.

Both paths write into the same <prefix>/raw_result.json, in the exact
format DatasetProcessor.run() produces, so `run_benchmark.py --regenerate-report`
works afterwards to produce the final report.

Usage:
    python tools/resume_agentic_eval.py \\
        --dataset dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl \\
        --prefix work_opencode_v2_UA \\
        --agent cvdp-opencode-agent:v2 \\
        --repo-root . \\
        --force-rerun door_lock_0001 \\
        [--extra-args "--network-name my-net --threads 4"] \\

Re-grading existing agent output with a newer dataset version (same agent
inputs, fixed harness), keeping the original run intact:
    python tools/resume_agentic_eval.py \\
        --dataset dataset/cvdp_v1.1.0_agentic_code_generation_no_commercial.jsonl \\
        --prefix work_opencode_6.1_v1 --copy-to work_opencode_6.1_v1_h110 \\
        --agent <agent-image> --only-eval --refresh-harness \\
        [--oss-sim-image osvb-cocotb:v2]
For commercial runs with a cocotb-2 dataset (v1.1.0+) also pass --new-harness
and a cocotb-2 capable --verif-eda-image.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import yaml


def compute_issue_path(prefix, id_str):
    """Mirrors DatasetProcessor.extract_datapoint()'s path derivation."""
    parts = id_str.split("_")
    repo_name = "cvdp_" + "_".join(parts[1:-1])
    issue = int(parts[-1])
    issue_path = os.path.join(prefix, repo_name, "harness", str(issue))
    reports_path = os.path.join(prefix, repo_name, "reports")
    return issue_path, reports_path, issue


def load_dataset(dataset_path):
    entries = []
    with open(dataset_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    return entries


def load_raw_result(prefix):
    raw_result_path = os.path.join(prefix, "raw_result.json")
    if os.path.exists(raw_result_path):
        with open(raw_result_path, "r") as f:
            try:
                return json.load(f), raw_result_path
            except json.JSONDecodeError:
                return {}, raw_result_path
    return {}, raw_result_path


def save_raw_result(raw_result_path, data):
    # Back up whatever was there before touching it.
    if os.path.exists(raw_result_path):
        shutil.copy2(raw_result_path, raw_result_path + ".bak")
    os.makedirs(os.path.dirname(raw_result_path) or ".", exist_ok=True)
    with open(raw_result_path, "w") as f:
        json.dump(data, f, indent=2)


def default_docker_timeout():
    """DOCKER_TIMEOUT from the environment or ./.env, as ConfigManager reads it."""
    value = os.environ.get("DOCKER_TIMEOUT")
    if value is None:
        try:
            from dotenv import dotenv_values
            value = dotenv_values(".env").get("DOCKER_TIMEOUT")
        except ImportError:
            pass
    return int(value) if value else None


def docker_cmd_volumes(issue_path, repo_root):
    """Mirrors Repository.docker_cmd()'s volume list."""
    path = os.path.abspath(issue_path)
    vols = []
    for d in ["docs", "rundir", "rtl", "verif", "src"]:
        vols += ["-v", f"{path}/{d}:/code/{d}"]
    llm_lib = os.path.join(os.path.abspath(repo_root), "src", "llm_lib")
    if os.path.isdir(llm_lib):
        vols += ["-v", f"{llm_lib}:/pysubj"]
    return vols


PLACEHOLDER_RE = re.compile(r"^__[A-Z_]+__$")


def infer_original_images(harness, issue_path):
    """
    Maps each image placeholder (__OSS_SIM_IMAGE__, __VERIF_EDA_IMAGE__, ...) used by
    the dataset harness to the image the original run resolved it to, read from the
    on-disk docker-compose.yml (`image:` per service) and Dockerfile (`FROM` lines).
    """
    values = {}

    new_compose = yaml.safe_load(harness.get("docker-compose.yml") or "") or {}
    old_compose_path = os.path.join(issue_path, "docker-compose.yml")
    if os.path.exists(old_compose_path):
        with open(old_compose_path, "r") as f:
            old_services = (yaml.safe_load(f) or {}).get("services") or {}
        for name, svc in (new_compose.get("services") or {}).items():
            image = str((svc or {}).get("image", ""))
            old_image = (old_services.get(name) or {}).get("image")
            if PLACEHOLDER_RE.match(image) and old_image and not PLACEHOLDER_RE.match(old_image):
                values.setdefault(image, old_image)

    new_from = [l.split()[1] for l in (harness.get("Dockerfile") or "").splitlines() if l.startswith("FROM ")]
    old_dockerfile_path = os.path.join(issue_path, "Dockerfile")
    if os.path.exists(old_dockerfile_path):
        with open(old_dockerfile_path, "r") as f:
            old_from = [l.split()[1] for l in f.read().splitlines() if l.startswith("FROM ")]
        # Multi-stage Dockerfiles: FROM lines correspond in order.
        for new_image, old_image in zip(new_from, old_from):
            if PLACEHOLDER_RE.match(new_image) and not PLACEHOLDER_RE.match(old_image):
                values.setdefault(new_image, old_image)

    return values


def refresh_harness(entry, issue_path, issue, repo_root, image_overrides=None, dry_run=False):
    """
    Rewrites the on-disk harness of an already agent-processed id from the
    dataset entry, leaving the agent's output (rtl/verif/docs/patch) alone.

    Needed when re-evaluating with a newer dataset version: evaluation reads
    docker-compose.yml, Dockerfile (services with `build: .`) and src/ from disk,
    which still hold the harness of the dataset the agent originally ran on.
    Files go through Repository.restore_files(), so template substitution and
    compose filtering match the original prepare().

    Image placeholders resolve to the images of the original run, not to the
    current config (which may point elsewhere), unless overridden through
    image_overrides ({"__OSS_SIM_IMAGE__": "osvb-cocotb:v2", ...}).
    The previous src/, docker-compose.yml and Dockerfile are moved to
    <issue_path>/harness_backup_<timestamp>/.
    """
    harness = entry.get("harness") or {}
    harness = harness.get("files", harness)
    if not harness:
        print(f"  [REFRESH] {entry['id']}: dataset entry has no harness, keeping the on-disk one")
        return

    images = infer_original_images(harness, issue_path)
    images.update(image_overrides or {})
    backup_dir = os.path.join(issue_path, f"harness_backup_{int(time.time())}")
    images_str = ", ".join(f"{k}={v}" for k, v in sorted(images.items())) or "none"
    print(f"  [REFRESH] {entry['id']}: {len(harness)} harness file(s) from dataset "
          f"(images: {images_str}), old ones -> {backup_dir}")
    if dry_run:
        return

    # Keep the network the original compose was generated with, so the refreshed
    # compose doesn't reference a different (possibly missing) external network.
    compose_path = os.path.join(issue_path, "docker-compose.yml")
    network_name = None
    if os.path.exists(compose_path):
        with open(compose_path, "r") as f:
            old_compose = yaml.safe_load(f) or {}
        network_name = (old_compose.get("networks") or {}).get("default", {}).get("name")

    os.makedirs(backup_dir)
    for item in ["src", "docker-compose.yml", "Dockerfile"]:
        path = os.path.join(issue_path, item)
        if os.path.exists(path):
            shutil.move(path, os.path.join(backup_dir, item))

    # src/__init__ pulls in the whole framework (nltk included): import only when needed.
    sys.path.insert(0, os.path.abspath(repo_root))
    from types import SimpleNamespace
    from src import commercial_eda, repository

    # restore_files() only needs these attributes; write_file() resolves paths
    # as <name>/harness/<id>/<file>, i.e. back into issue_path.
    stub = SimpleNamespace(
        name=os.path.dirname(os.path.dirname(issue_path)),
        id=issue,
        network_name=network_name,
        requires_eda_license=commercial_eda.datapoint_requires_eda_license(entry),
    )
    stub.write_file = lambda filename, content: repository.Repository.write_file(stub, filename, content)

    # restore_files() looks apply_template_substitution up as a module global:
    # resolve our image placeholders first, leave the rest to the config.
    config_substitution = repository.apply_template_substitution

    def substitution(content):
        for placeholder, image in images.items():
            content = content.replace(placeholder, image)
        return config_substitution(content)

    repository.apply_template_substitution = substitution
    try:
        repository.Repository.restore_files(stub, harness)
    finally:
        repository.apply_template_substitution = config_substitution


def ensure_external_networks(issue_paths, dry_run=False):
    """
    Creates the external networks the on-disk compose files reference, like
    network_util.create_docker_network() does for a normal run. run_benchmark.py
    removes its network when the run ends, so evaluating a finished run would
    otherwise fail with "network ... declared as external, but could not be found".
    Returns the networks created here, for remove_networks().
    """
    names = set()
    for issue_path in issue_paths:
        compose_path = os.path.join(issue_path, "docker-compose.yml")
        if not os.path.exists(compose_path):
            continue
        with open(compose_path, "r") as f:
            networks = (yaml.safe_load(f) or {}).get("networks") or {}
        for key, net in networks.items():
            if isinstance(net, dict) and net.get("external"):
                names.add(net.get("name", key))

    created = []
    for name in sorted(names):
        exists = subprocess.run(
            ["docker", "network", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"],
            capture_output=True, text=True,
        ).stdout.strip()
        if exists:
            continue
        print(f"Creating missing external network: {name}")
        if dry_run:
            continue
        subprocess.run(["docker", "network", "create", name, "--driver", "bridge"],
                       check=True, stdout=subprocess.DEVNULL)
        created.append(name)
    return created


def remove_networks(names):
    for name in names:
        print(f"Removing network created for this evaluation: {name}")
        subprocess.run(["docker", "network", "rm", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_harness_for_service(compose_path, issue_path, reports_path, issue,
                             service, repo_name, repo_root, num_services=1, dry_run=False,
                             timeout=None):
    """Runs `docker compose run` for one service, mirroring Repository.log_docker()."""
    formatted_repo = "".join(
        c.lower() if c.isalnum() or c in "-_" else "_" for c in repo_name
    )
    if not formatted_repo[0].isalnum():
        formatted_repo = "p" + formatted_repo
    project_name = f"{formatted_repo}_{issue}_{int(time.time())}"

    os.makedirs(reports_path, exist_ok=True)
    # Same naming as Repository.obj_harness(): "<id>.txt" for a single service,
    # "<id>_<service>.txt" when there's more than one.
    suffix = f"_{service}" if num_services > 1 else ""
    logfile = os.path.join(reports_path, f"{issue}{suffix}.txt")

    cmd = [
        "docker", "compose", "-f", compose_path, "-p", project_name,
        "run", "--rm",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-e", "HOME=/code/rundir",
        "-w", "/code/rundir",
    ]
    cmd += docker_cmd_volumes(issue_path, repo_root)
    cmd += [service]

    print(f"    $ {' '.join(cmd)}")
    if dry_run:
        return {"result": 0, "log": logfile, "error_msg": "DRY-RUN", "execution": 0.0}

    start = time.time()
    timed_out = False
    with open(logfile, "w") as log_f:
        try:
            proc = subprocess.run(cmd, stdout=log_f, stderr=subprocess.STDOUT, timeout=timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            # Same failure mode as Repository.exec_timeout(): the container would
            # otherwise keep running (e.g. a testbench stuck in `while (!done)`).
            timed_out = True
            returncode = 1
            print(f"    [TIMEOUT] {service} exceeded {timeout}s, killing project {project_name}")
            subprocess.run(
                ["docker", "compose", "-f", compose_path, "-p", project_name, "kill"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            log_f.write(f"\nTimeout for {service} ({timeout}s) expired\n")
    duration = time.time() - start

    # Best-effort cleanup of the throwaway network/project resources. Services
    # with `build: .` leave a <project>-<service> image behind, removed like
    # Repository.log_docker()'s cleanup does.
    subprocess.run(["docker", "rmi", f"{project_name}-{service}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(
        f"docker network ls --filter name={formatted_repo}_{issue} -q "
        f"| xargs -r docker network rm 2>/dev/null || true",
        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    if timed_out:
        error_msg = f"harness timed out after {timeout}s"
    else:
        error_msg = None if returncode == 0 else f"harness exited {returncode}"

    return {
        "result": returncode,
        "log": logfile,
        "error_msg": error_msg,
        "execution": duration,
    }


def evaluate_done_id(id_str, categories, issue_path, reports_path, issue,
                      repo_name, repo_root, dry_run=False, timeout=None):
    """Evaluates an already agent-processed id directly against disk state."""
    compose_path = os.path.join(issue_path, "docker-compose.yml")
    if not os.path.exists(compose_path):
        print(f"  [SKIP] {id_str}: no docker-compose.yml found, needs manual review")
        return None

    with open(compose_path, "r") as f:
        compose_data = yaml.safe_load(f)
    services = list(compose_data.get("services", {}).keys())
    if not services:
        print(f"  [SKIP] {id_str}: docker-compose.yml has no services, needs manual review")
        return None

    tests = []
    errors = 0
    for service in services:
        print(f"  [EVAL] {id_str} :: service '{service}'")
        res = run_harness_for_service(
            compose_path, issue_path, reports_path, issue, service,
            repo_name, repo_root, num_services=len(services), dry_run=dry_run,
            timeout=timeout,
        )
        tests.append(res)
        errors += 1 if res["result"] != 0 else 0

    agent_logfile = os.path.join(reports_path, f"{issue}_agent.txt")

    return {
        "category": categories[0],
        "difficulty": categories[1],
        "tests": tests,
        "errors": errors,
        "skipped": 0,
        "executed": 1,
        "status": "failed" if errors > 0 else "passed",
        "agent_logfile": agent_logfile if os.path.exists(agent_logfile) else None,
    }


def rerun_not_done_id(id_str, dataset_path, prefix, agent_image, extra_args, dry_run=False):
    """Re-runs prepare+agent+eval for an id whose agent never completed."""
    cmd = [
        sys.executable, "run_benchmark.py",
        "-f", dataset_path, "-l", "-g", agent_image, "-p", prefix,
        "--id", id_str,
    ]
    if extra_args:
        cmd += extra_args.split()

    print(f"  [RERUN] {id_str}")
    print(f"    $ {' '.join(cmd)}")
    if dry_run:
        return
    subprocess.run(cmd, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True, help="Path to the dataset .jsonl")
    parser.add_argument("--prefix", required=True, help="Same --prefix used in the original run")
    parser.add_argument("--agent", required=True, help="Agent image, for re-running incomplete ids")
    parser.add_argument("--repo-root", default=".", help="Path to the master_tesis_cvdp checkout (for /pysubj mount)")
    parser.add_argument("--extra-args", default="", help="Extra flags to forward to run_benchmark.py for incomplete ids (e.g. \"--network-name my-net --threads 4\")")
    parser.add_argument("--force-rerun", default="", help="Comma-separated dataset ids to always treat as incomplete (e.g. the one(s) actively running when the session died — their <id>_agent.txt exists but is truncated, so it can't be told apart from a clean run automatically)")
    parser.add_argument("--only-ids", default="", help="Comma-separated dataset ids to process; everything else in --dataset is skipped entirely. Use this to resume/re-evaluate a single datapoint (combine with --force-rerun to force a full prepare+agent+eval, or leave alone to just re-run the harness/eval on an already agent-processed id)")
    parser.add_argument("--timeout", type=int, default=default_docker_timeout(), help="Per-service harness timeout in seconds (default: DOCKER_TIMEOUT from env/.env, else none)")
    parser.add_argument("--refresh-harness", action="store_true", help="Before evaluating an already agent-processed id, rewrite its on-disk harness (src/, docker-compose.yml, Dockerfile) from --dataset. Use it to re-grade existing agent output with a newer dataset version whose agent inputs are unchanged; without it the harness from the original run is used. Image placeholders resolve to the images of the original run unless --oss-sim-image/--verif-eda-image are given")
    parser.add_argument("--oss-sim-image", default="", help="With --refresh-harness: image for __OSS_SIM_IMAGE__ instead of the one the original run used (e.g. osvb-cocotb:v2 to re-grade a cocotb-v1 run with a cocotb-2 harness)")
    parser.add_argument("--verif-eda-image", default="", help="With --refresh-harness: image for __VERIF_EDA_IMAGE__ instead of the one the original run used")
    parser.add_argument("--new-harness", action="store_true", help="With --refresh-harness: the dataset harness targets cocotb 2 (v1.1.0+). Sets NEW_HARNESS, so EDA services whose cocotb runner passes -timescale itself don't also get the XRUNOPTS default (xrun would fail with OPTNOML)")
    parser.add_argument("--copy-to", default="", help="Copy --prefix to this new directory first and work only on the copy, leaving the original run (files, logs, raw_result.json) untouched. Fails if the directory already exists")
    parser.add_argument("--dry-run", action="store_true", help="Print what would run without executing docker/run_benchmark.py")
    parser.add_argument("--only-rerun", action="store_true", help="Skip phase 1 (evaluating already agent-processed ids) - useful once raw_result.json already has them from a previous invocation, and you only want to (re)process the incomplete ids")
    parser.add_argument("--only-eval", action="store_true", help="Skip phase 2 (rerunning incomplete ids) - only evaluate the already agent-processed ones")
    args = parser.parse_args()

    force_rerun_ids = {i.strip() for i in args.force_rerun.split(",") if i.strip()}
    only_ids = {i.strip() for i in args.only_ids.split(",") if i.strip()}
    if args.new_harness:
        # Read by src.config_manager on its (lazy) import in refresh_harness();
        # the environment takes precedence over .env.
        os.environ["NEW_HARNESS"] = "true"
    image_overrides = {}
    if args.oss_sim_image:
        image_overrides["__OSS_SIM_IMAGE__"] = args.oss_sim_image
    if args.verif_eda_image:
        image_overrides["__VERIF_EDA_IMAGE__"] = args.verif_eda_image

    if args.copy_to:
        if os.path.exists(args.copy_to):
            sys.exit(f"ERROR: --copy-to {args.copy_to} already exists, refusing to overwrite it")
        print(f"Copying {args.prefix} -> {args.copy_to} (the original is left untouched)")
        if not args.dry_run:
            shutil.copytree(args.prefix, args.copy_to, symlinks=True)
            args.prefix = args.copy_to

    entries = load_dataset(args.dataset)
    print(f"Loaded {len(entries)} datapoints from {args.dataset}")

    if only_ids:
        found_ids = {e["id"] for e in entries}
        missing = only_ids - found_ids
        if missing:
            print(f"WARNING: --only-ids not found in dataset, ignoring: {sorted(missing)}")
        entries = [e for e in entries if e["id"] in only_ids]
        print(f"--only-ids: restricting to {len(entries)} datapoint(s)")

    done, not_done, no_patch_but_done, skipped, absent = [], [], [], [], []
    for entry in entries:
        id_str = entry["id"]
        issue_path, reports_path, issue = compute_issue_path(args.prefix, id_str)

        # With --only-eval nothing gets (re)run, so ids this run never touched
        # (e.g. V2 ids of a dataset that also covers a V1-only run) are just noise.
        if args.only_eval and not os.path.isdir(issue_path) and id_str not in force_rerun_ids:
            absent.append(id_str)
            continue

        if id_str in force_rerun_ids:
            not_done.append(entry)
            continue

        # "Done" means the agent step ran to completion, successfully or not -
        # its full log was written. This is NOT the same as agent_changes.patch,
        # which only exists if the agent actually modified files: an agent that
        # failed cleanly (crashed, produced nothing) still finished and should
        # not be re-run just because it produced no diff.
        agent_logfile = os.path.join(reports_path, f"{issue}_agent.txt")
        if os.path.exists(agent_logfile) and os.path.getsize(agent_logfile) > 0:
            done.append((entry, issue_path, reports_path, issue))
            if not os.path.exists(os.path.join(issue_path, "agent_changes.patch")):
                no_patch_but_done.append(id_str)
        else:
            not_done.append(entry)

    print(f"\n{len(done)} datapoint(s) already agent-processed -> will evaluate only")
    print(f"{len(not_done)} datapoint(s) incomplete -> will rerun prepare+agent+eval\n")
    if absent:
        print(f"NOTE: {len(absent)} dataset id(s) are not part of this run (no harness dir), ignored\n")
    if no_patch_but_done:
        print(f"NOTE: {len(no_patch_but_done)} of the 'done' ids have an agent log but no "
              f"agent_changes.patch (agent ran but produced no file changes - will evaluate "
              f"as-is, almost certainly a fail): {no_patch_but_done}\n")

    raw_result, raw_result_path = load_raw_result(args.prefix)

    # 1) Evaluate the already-completed ones directly, without touching prepare().
    if not args.only_rerun:
        # refresh_harness() keeps the compose networks, so they can be read up front.
        created_networks = ensure_external_networks([p for _, p, _, _ in done], dry_run=args.dry_run)
        try:
            for entry, issue_path, reports_path, issue in done:
                id_str = entry["id"]
                parts = id_str.split("_")
                repo_name = "cvdp_" + "_".join(parts[1:-1])
                if args.refresh_harness:
                    refresh_harness(entry, issue_path, issue, args.repo_root,
                                    image_overrides=image_overrides, dry_run=args.dry_run)
                result = evaluate_done_id(
                    id_str, entry["categories"], issue_path, reports_path, issue,
                    repo_name, args.repo_root, dry_run=args.dry_run,
                    timeout=args.timeout,
                )
                if result is None:
                    skipped.append(id_str)
                    continue
                raw_result[id_str] = result
                if not args.dry_run:
                    save_raw_result(raw_result_path, raw_result)
        finally:
            remove_networks(created_networks)
    else:
        print(f"--only-rerun: skipping evaluation of the {len(done)} already agent-processed id(s)\n")

    # 2) Rerun the incomplete ones through the normal per-id pipeline.
    #    run_benchmark.py --id already appends its result into raw_result.json itself.
    if not args.only_eval:
        for entry in not_done:
            rerun_not_done_id(
                entry["id"], args.dataset, args.prefix, args.agent,
                args.extra_args, dry_run=args.dry_run,
            )
    else:
        print(f"--only-eval: skipping rerun of the {len(not_done)} incomplete id(s)\n")

    evaluated_ids = [
        e["id"] for e, *_ in done
        if e["id"] not in skipped and e["id"] not in no_patch_but_done
    ]
    not_done_ids = [e["id"] for e in not_done]

    def print_list(title, ids):
        print(f"\n{title} ({len(ids)}):")
        for i in ids:
            print(f"  - {i}")

    print("\n=== Summary ===")
    print_list("Valutati", evaluated_ids)
    print_list("Agente mai partito", not_done_ids)
    print_list("Agente fallito (log presente ma nessun agent_changes.patch)", no_patch_but_done)
    if skipped:
        print_list("Da rivedere a mano (no docker-compose.yml/services)", skipped)
    print(f"\nResults merged into: {raw_result_path}")
    print("Once everything is done, generate the final report with:")
    print(f"  python run_benchmark.py -f {args.dataset} -l -g {args.agent} -p {args.prefix} --regenerate-report")


if __name__ == "__main__":
    main()
