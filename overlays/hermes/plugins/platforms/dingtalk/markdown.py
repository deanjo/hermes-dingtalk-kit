"""DingTalk markdown normalization helpers."""

import re


def normalize_markdown(text: str) -> str:
    """Normalize markdown for DingTalk's parser."""
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        is_numbered = re.match(r"^\d+\.\s", line.strip())
        if is_numbered and i > 0:
            prev = lines[i - 1]
            if prev.strip() and not re.match(r"^\d+\.\s", prev.strip()):
                out.append("")
        if line.strip().startswith("```") and line != line.lstrip():
            indent = len(line) - len(line.lstrip())
            line = line[indent:]
        out.append(line)
    return "\n".join(out)
