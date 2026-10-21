"""Generate GitLab Duo Code Review's custom instructions from the Rulebook (FR-BUILTIN-1).

    python -m vallum.review_instructions          # rewrite .gitlab/duo/mr-review-instructions.yaml
    python -m vallum.review_instructions --check  # fail if the file is out of date (unit test does this)
"""

import re
import sys

import yaml

from vallum.config import ROOT, VALLUM_DIR

OUT = ROOT / ".gitlab" / "duo" / "mr-review-instructions.yaml"
HEADER = "# Generated from vallum/RULEBOOK.md by `python -m vallum.review_instructions`. Do not edit by hand.\n"


def sections() -> dict[str, list[str]]:
    text = (VALLUM_DIR / "RULEBOOK.md").read_text(encoding="utf-8")
    out, current = {}, None
    for line in text.splitlines():
        m = re.match(r"^## \d+\. (.+)$", line)
        if m:
            current = m.group(1).strip()
            out[current] = []
        elif current and line.startswith("- "):
            out[current].append(line[2:].strip())
    return out


class _Literal(yaml.SafeDumper):
    pass


def _str(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_Literal.add_representer(str, _str)


def render() -> str:
    s = sections()

    def block(*names):
        return "\n".join(f"- {rule}" for n in names for rule in s.get(n, []))

    doc = {"instructions": [
        {"name": "Vallum Rulebook: safety and security",
         "instructions": "This project ships with Vallum. Review against its Rulebook. Flag any change that breaks these rules:\n"
                         + block("Safety", "Security") + "\n"},
        {"name": "Vallum Rulebook: feature modules",
         "fileFilters": ["app/features/**"],
         "instructions": block("Quality") + "\n- Every route of a new feature depends on flags.require(\"<flag>\").\n"
                         "- Write routes need the API key; inputs are validated (bad input answers 422).\n"},
        {"name": "Vallum Rulebook: Feature Cards",
         "fileFilters": ["promises/**"],
         "instructions": "- Promises may only be added or tightened. Flag any deleted promise, raised limit, lowered severity or lowered tier.\n"
                         "- Every 'done when' line of the request needs a promise; new behavior promises must exercise the new code.\n"
                         "- owns.routes and owns.files must list everything the change adds.\n"},
        {"name": "Vallum Rulebook: protected paths",
         "fileFilters": ["vallum/**", ".gitlab-ci.yml", ".gitlab/duo/**", "skills/**", "AGENTS.md", "CODEOWNERS",
                         "promises/service.yaml", "promises/learned/**"],
         "instructions": "These are protected paths. Only the project owner may change them; the Gatekeeper refuses agent changes. "
                         "Point out any change here that weakens a check, a cap or a rule.\n"},
    ]}
    return HEADER + yaml.dump(doc, Dumper=_Literal, sort_keys=False, allow_unicode=True, width=120)


def main():
    text = render()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current.replace("\r\n", "\n") != text:
            raise SystemExit(f"{OUT} is out of date: run python -m vallum.review_instructions")
        print("up to date")
        return
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
