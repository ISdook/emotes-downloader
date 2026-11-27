"""Release helper used by GitHub Actions.

    python tools/release_notes.py version        -> prints __version__ from emotes_downloader.py
    python tools/release_notes.py notes 1.1.0    -> prints the CHANGELOG.md section for 1.1.0
                                                    (fails if there is none)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def version():
    src = (ROOT / "emotes_downloader.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', src, re.M).group(1)


def notes(ver):
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    head = re.compile(r"^##\s*\[?v?(\d+(?:\.\d+){1,2})\]?")
    out, inside = [], False
    for line in text.splitlines():
        m = head.match(line.strip())
        if m:
            if inside:
                break
            inside = m.group(1) == ver
            continue
        if inside:
            out.append(line)
    body = "\n".join(out).strip()
    if not body:
        sys.exit(f"CHANGELOG.md has no entry for v{ver}. Add a '## v{ver} — date' section first.")
    return body


if __name__ == "__main__":
    if sys.argv[1] == "version":
        print(version())
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(notes(sys.argv[2]))
