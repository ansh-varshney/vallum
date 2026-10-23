"""Generate the code map.

    python -m vallum.codemap [--root DIR] [--check] [--stdout]
"""

import argparse
import sys
from pathlib import Path

from vallum.codemap import generate, output_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--check", action="store_true", help="fail if the committed map is out of date")
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    text = generate(root)
    if args.stdout:
        sys.stdout.write(text)
        return
    out = output_path(root)
    if args.check:
        current = out.read_text(encoding="utf-8").replace("\r\n", "\n") if out.exists() else ""
        if current != text:
            raise SystemExit(f"{out} is out of date: run python -m vallum.codemap")
        print("code map up to date")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
