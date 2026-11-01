"""Generate translations/en.json from strings.json, resolving [%key:...%] refs."""

import json
from pathlib import Path
import re

import homeassistant

ROOT = Path(__file__).resolve().parent.parent
COMP = ROOT / "custom_components" / "swann"
common = json.loads((Path(homeassistant.__file__).parent / "strings.json").read_text())
REF = re.compile(r"\[%key:([^%]+)%\]")


def lookup(path: str) -> str:
    node = {"common": common["common"]}
    for part in path.split("::"):
        node = node[part]
    return node


def resolve(obj):
    if isinstance(obj, dict):
        return {k: resolve(v) for k, v in obj.items()}
    if isinstance(obj, str):
        return REF.sub(lambda m: lookup(m.group(1)), obj)
    return obj


strings = json.loads((COMP / "strings.json").read_text())
out = COMP / "translations" / "en.json"
out.write_text(json.dumps(resolve(strings), indent=2, ensure_ascii=False) + "\n")
print(f"wrote {out}")
