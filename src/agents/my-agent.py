import json
import os
import re
import subprocess
import sys
from pathlib import Path
import anthropic

CODE_ROOT = Path("/code")
PROMPT_JSON_PATH = CODE_ROOT / "prompt.json"
MAX_ITER = 3
# LLM_MODEL is "<provider>/<model>" (e.g. vertex_ai/claude-sonnet-4-6); this agent only needs the model id.
MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6").rsplit("/", 1)[-1]
FILE_DELIMITER = "// ===FILE: {filename} ==="

VERTEX_PROJECT = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
VERTEX_LOCATION = os.environ.get("ANTHROPIC_VERTEX_LOCATION", "us-east5")

def make_client():
    """Crea il client Anthropic corretto in base all'ambiente (Vertex vs direct API)."""
    if VERTEX_PROJECT:
        token = os.environ.get("GOOGLE_ACCESS_TOKEN", "")
        if not token:
            raise RuntimeError(
                "GOOGLE_ACCESS_TOKEN non trovato. "
                "Assicurati che dataset_processor.py stia generando il token sull'host."
            )
        import datetime
        from google.oauth2.credentials import Credentials
        creds = Credentials(token=token)
        creds.expiry = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        client = anthropic.AnthropicVertex(project_id=VERTEX_PROJECT, region=VERTEX_LOCATION)
        client.credentials = creds
        return client
    return anthropic.Anthropic()

def read_prompt_payload() -> dict:
    """Read the full CVDP prompt payload from /code/prompt.json."""
    try:
        with open(PROMPT_JSON_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
    except Exception as e:
        print(f"Error reading prompt.json: {e}")
        return {}

def read_prompt() -> str:
    """Read only the prompt text from the CVDP payload."""
    payload = read_prompt_payload()
    prompt = payload.get("prompt", "")
    return prompt if isinstance(prompt, str) else ""

def get_rtl_files() -> dict:
    rtl_dir = "/code/rtl"
    files = {}
    for fname in os.listdir(rtl_dir):
        if fname.endswith(".v") or fname.endswith(".sv"):
            path = os.path.join(rtl_dir, fname)
            with open(path) as f:
                files[fname] = f.read()
    return files

def write_rtl_file(fname: str, content: str):
    with open(f"/code/rtl/{fname}", "w") as f:
        f.write(content)

def run_simulation():
    lint_ok, lint_log = run_lint()
    if not lint_ok:
        return False, "[lint]\n" + lint_log

    compile_ok, compile_log = run_compile()
    if not compile_ok:
        return True, "[lint]\n" + lint_log + "[compile inconclusive]\n" + compile_log

    return True, "[lint]\n" + lint_log + "\n[compile]\n" + compile_log

def run_lint():
    rtl_dir = CODE_ROOT / "rtl"

    if not rtl_dir.exists():
        return False, "RTL directory not found."

    rtl_files = [
        str(p) for p in rtl_dir.iterdir()
        if p.is_file() and p.suffix in {".v", ".sv"}
    ]

    if not rtl_files:
        return False, "No RTL files to lint."

    try:
        result = subprocess.run(
            ["verilator", "--lint-only", "-Wall", "-Wno-fatal", *rtl_files],
            capture_output=True,
            text=True,
            timeout=60
        )
        log = (result.stdout or "") + (result.stderr or "")
        passed = result.returncode == 0
    except FileNotFoundError:
        return False, "Verilator not found in container."
    except subprocess.TimeoutExpired as e:
        log = (e.stdout or "") + (e.stderr or "") + "\nTimeout after 60s"
        return False, log

    return passed, log

def run_compile():
    rtl_dir = CODE_ROOT / "rtl"

    if not rtl_dir.exists():
        return False, "RTL directory not found."

    rtl_files = [
        str(p) for p in rtl_dir.iterdir()
        if p.is_file() and p.suffix in {".v", ".sv"}
    ]

    if not rtl_files:
        return False, "No RTL files to compile."

    try:
        result = subprocess.run(
            ["iverilog", "-tnull", *rtl_files],
            capture_output=True,
            text=True,
            timeout=60
        )
        log = (result.stdout or "") + (result.stderr or "")
        passed = result.returncode == 0
    except FileNotFoundError:
        return False, "iverilog not found in container."
    except subprocess.TimeoutExpired as e:
        log = (e.stdout or "") + (e.stderr or "") + "\nTimeout after 60s"
        passed = False

    return passed, log


def extract_target_filenames(prompt_text: str) -> list:
    """Estrae i nomi dei file RTL target dal testo del prompt."""
    seen = set()
    result = []

    def add(name):
        if name not in seen:
            seen.add(name)
            result.append(name)

    # 1. Path rtl/ espliciti tra backtick: `rtl/foo.sv`
    for m in re.findall(r'`rtl/(\w+\.(?:sv|v))`', prompt_text):
        add(m)

    # 2. File .sv/.v tra backtick: `foo.sv`
    for m in re.findall(r'`(\w+\.(?:sv|v))`', prompt_text):
        add(m)

    # 3. File .sv/.v generici nel testo (non in mezzo a parole)
    for m in re.findall(r'\b(\w+\.(?:sv|v))\b', prompt_text):
        add(m)

    if result:
        return result

    # 4. Nome del modulo che PRECEDE "module": "`foo` module"
    m = re.search(r'`(\w+)`\s+module', prompt_text)
    if m:
        return [m.group(1) + ".sv"]

    # 5. "Design/Implement a/an `foo`"
    m = re.search(r'(?:Design|design|Implement|implement)\s+(?:an?\s+)?`(\w+)`', prompt_text)
    if m:
        return [m.group(1) + ".sv"]

    return ["design.sv"]

def parse_multi_file_response(text: str, target_files: list) -> dict:
    """Estrae il contenuto di ciascun file dalla risposta del modello."""
    files = {}
    for fname in target_files:
        delimiter = re.escape(FILE_DELIMITER.format(filename=fname))
        pattern = rf'{delimiter}\n(.*?)(?=// ===FILE:|\Z)'
        m = re.search(pattern, text, re.DOTALL)
        if m:
            files[fname] = m.group(1).strip()
        else:
            print(f"[my-agent.py] WARNING: file {fname} non trovato nella risposta del modello.")
    return files

def build_format_instructions(target_files: list) -> str:
    """Costruisce le istruzioni di formato per il modello."""
    lines = []
    for fname in target_files:
        lines.append(FILE_DELIMITER.format(filename=fname))
        lines.append(f"<complete SystemVerilog/Verilog content for {fname}>")
    return "\n".join(lines)

def call_generator(task: str, context_files: dict, target_files: list) -> dict:
    """Singola chiamata LLM che genera tutti i file target in una volta."""
    client = make_client()

    context_str = "\n\n".join(
        f"// File: {name}\n{content}"
        for name, content in context_files.items()
    ) if context_files else "(no existing files)"

    target_list = "\n".join(f"- {f}" for f in target_files)
    format_instructions = build_format_instructions(target_files)

    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system="You are an expert RTL designer. Generate only Verilog/SystemVerilog code, no explanations.",
        messages=[{
            "role": "user",
            "content": f"""{task}

Existing files:
{context_str}

Generate ALL of the following files:
{target_list}

Use EXACTLY this format for each file (copy the delimiter line verbatim):
{format_instructions}

Output ONLY the file contents in the format above. No prose, no markdown fences."""
        }]
    )

    return parse_multi_file_response(response.content[0].text, target_files)

def call_refactorer(task: str, current_files: dict, error_log: str, target_files: list) -> dict:
    """Singola chiamata LLM che corregge tutti i file sulla base del log di errore."""
    client = make_client()

    current_str = "\n\n".join(
        FILE_DELIMITER.format(filename=name) + "\n" + content
        for name, content in current_files.items()
    )

    format_instructions = build_format_instructions(target_files)

    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system="You are an expert RTL debugger. Fix the provided Verilog code based on the lint and compile feedback.",
        messages=[{
            "role": "user",
            "content": f"""Task: {task}

Current files:
{current_str}

Simulation error:
{error_log}

Output ALL corrected files using EXACTLY this format (copy the delimiter line verbatim):
{format_instructions}

Output ONLY the corrected file contents. No prose, no markdown fences."""
        }]
    )

    return parse_multi_file_response(response.content[0].text, target_files)

def main():
    print("[my-agent.py] Starting My Agentic Workflow ...")

    payload = read_prompt_payload()
    task = read_prompt()
    if not isinstance(task, str) or not task:
        print("[my-agent.py] No prompt found in /code/prompt.json. Exiting.", file=sys.stderr)
        print("[my-agent.py] Payload content: ", payload, file=sys.stderr)
        sys.exit(1)

    target_files = extract_target_filenames(task)
    print(f"[my-agent.py] Target files: {target_files}")

    context_files = get_rtl_files()

    """ print("[my-agent.py] /code contents:", os.listdir("/code"))
    print("[my-agent.py] /code/rtl contents:", os.listdir("/code/rtl") if os.path.exists("/code/rtl") else [])
    print("[my-agent.py] /code/verif contents:", os.listdir("/code/verif") if os.path.exists("/code/verif") else [])
    print("[my-agent.py] /code/rundir contents:", os.listdir("/code/rundir") if os.path.exists("/code/rundir") else [])
    """
    # --- ITERAZIONE 0: generazione iniziale ---
    print("[my-agent.py] Generator: generazione iniziale...")
    current_files = call_generator(task, context_files, target_files)

    if not current_files:
        print("[my-agent.py] Generator non ha prodotto nessun file. Exiting.", file=sys.stderr)
        sys.exit(1)

    for fname, content in current_files.items():
        write_rtl_file(fname, content)

    # --- LOOP: refactoring iterativo ---
    for i in range(MAX_ITER):
        passed, log = run_simulation()

        if passed:
            print(f"[my-agent.py] Passato all'iterazione {i}!")
            return

        print(f"[my-agent.py] Iterazione {i+1}/{MAX_ITER}: fallito, chiamo Refactorer...")

        with open(f"/code/rtl/log_{i}.txt", "w") as f:
            f.write(log)

        fixed_files = call_refactorer(task, current_files, log, target_files)

        if not fixed_files:
            print("[my-agent.py] Refactorer non ha prodotto output, rigenerazione completa...")
            fixed_files = call_generator(task, current_files, target_files)

        current_files.update(fixed_files)
        for fname, content in fixed_files.items():
            write_rtl_file(fname, content)

    # Ultima simulazione
    passed, _ = run_simulation()
    print("Risultato finale:", "PASS" if passed else "FAIL")

    #tool calls!!!
    #Too many logic hardcoded - more freedom to the LLMs

if __name__ == "__main__":
    main()
