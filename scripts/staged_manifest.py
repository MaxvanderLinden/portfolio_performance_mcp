"""Read a staging manifest for promote-staged.sh. Standard library only; run with `python3 -I`.

    staged_manifest.py field <manifest> <name>   print one top-level text field
    staged_manifest.py show <manifest>           print the staged changes for review

Names and notes in the manifest come from tool calls and broker imports, so anything that is not
plainly printable (escape sequences, newlines, bidi overrides) is shown as "?": it must not be
able to disguise the changes being approved.
"""

from __future__ import annotations

import json
import sys

HIDDEN = {"uuid", "type_label", "security_uuid", "classification_id", "id"}


def clean(value: object) -> str:
    return "".join(ch if ch.isprintable() else "?" for ch in str(value))


def lines(details: object, indent: str = "      ") -> list[str]:
    """Nested details as indented `key: value` lines; money on one line, empty values left out."""
    if not isinstance(details, dict):
        return [indent + clean(details)]
    out: list[str] = []
    for key, value in details.items():
        if value is None or key in HIDDEN:
            continue
        if isinstance(value, dict) and set(value) == {"amount", "currency"}:
            out.append(f"{indent}{clean(key)}: {clean(value['currency'])} {clean(value['amount'])}")
        elif isinstance(value, dict):
            out.append(f"{indent}{clean(key)}:")
            out.extend(lines(value, indent + "  "))
        else:
            out.append(f"{indent}{clean(key)}: {clean(value)}")
    return out


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # a name must never abort the review (e.g. LANG=C)
        sys.stdout.reconfigure(errors="backslashreplace")
    command, path = argv[0], argv[1]
    with open(path, encoding="utf-8") as stream:
        manifest = json.load(stream)
    if command == "field":
        value = manifest[argv[2]]
        if not isinstance(value, str):
            raise SystemExit(f"{argv[2]} is not text")
        print(value)
    elif command == "show":
        print(f"Staged changes ({len(manifest['changes'])}):")
        for change in manifest["changes"]:
            print(f"  {clean(change['at'])}  {clean(change['description'])}")
            print(f"      changes: {clean(change['delta'])}")
            print("\n".join(lines(change.get("details", {}))))
    else:
        raise SystemExit(f"unknown command {command!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
