import os
import re
from typing import Optional


_SV_CONSTRUCTS = [
    ("module ", "endmodule"),
    ("interface ", "endinterface"),
    ("package ", "endpackage"),
    ("program ", "endprogram"),
]

_SV_KEYWORDS = {
    "logic", "always_ff", "always_comb", "always_latch",
    "typedef", "enum", "struct", "endinterface", "endpackage",
}


def _extract_best_fenced_block(content: str) -> Optional[str]:
    pattern = re.compile(r"```(?:[ \t]*([A-Za-z0-9_+-]+))?[ \t]*\n?(.*?)```", re.DOTALL)
    matches = pattern.findall(content)
    if not matches:
        return None

    scored_blocks = []
    for lang, block in matches:
        lang = (lang or "").strip().lower()
        block = block.strip()
        if not block:
            continue

        score = 0
        if lang in {"verilog", "systemverilog", "sv"}:
            score += 100
        if "module " in block and "endmodule" in block:
            score += 50
        elif "module " in block:
            score += 10
        if any(kw in block for kw in _SV_KEYWORDS):
            score += 20

        scored_blocks.append((score, block))

    if not scored_blocks:
        return None

    scored_blocks.sort(key=lambda item: item[0], reverse=True)
    return scored_blocks[0][1]


def _truncate_to_module(content: str) -> str:
    for start_kw, end_kw in _SV_CONSTRUCTS:
        start = content.find(start_kw)
        end = content.rfind(end_kw)
        if start >= 0 and end >= 0:
            return content[start : end + len(end_kw)].strip()
    return content.strip()


def output_cleanup(content: str, files: Optional[list] = None, rtl_only: bool = True) -> str:
    content = content.strip()

    # Se non siamo in modalità RTL diretta, non fare cleanup aggressivo
    if not rtl_only:
        return content

    if files and isinstance(files, list) and len(files) == 1:
        target = str(files[0])
        lines = content.splitlines()
        if lines:
            first = lines[0].strip()
            if first == target:
                content = "\n".join(lines[1:]).lstrip()
            elif (first.endswith('.sv') or first.endswith('.v')) and ('/' in first or first == os.path.basename(first)):
                content = "\n".join(lines[1:]).lstrip()

    fenced = _extract_best_fenced_block(content)
    if fenced is not None:
        content = fenced

    content = _truncate_to_module(content)

    return content