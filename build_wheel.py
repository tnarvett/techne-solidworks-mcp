"""Build solidworks_mcp-<version>-py3-none-any.whl from src/ with the standard library only.

    python build_wheel.py          (any Python 3.10+, e.g. `uv run python build_wheel.py`)

The version comes from src/solidworks_mcp/__init__.py; the wheel lands next to this file.
"""

import base64
import hashlib
import os
import re
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
PKG = os.path.join(SRC, "solidworks_mcp")

METADATA = """Metadata-Version: 2.4
Name: solidworks-mcp
Version: {version}
Summary: SolidWorks MCP (hjbaard, MIT) + local run_api_script extension + Techne tools
Requires-Python: >=3.11
License-File: LICENSE
Requires-Dist: pywin32>=306; sys_platform == "win32"
Requires-Dist: comtypes>=1.4; sys_platform == "win32"
Requires-Dist: mcp<2,>=1.26
"""
WHEEL = "Wheel-Version: 1.0\nGenerator: techne-build_wheel\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
ENTRY = "[console_scripts]\nsolidworks-mcp = solidworks_mcp.server:main\n"


def digest(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def main() -> str:
    init = open(os.path.join(PKG, "__init__.py"), encoding="utf-8").read()
    version = re.search(r'__version__\s*=\s*"([^"]+)"', init).group(1)
    dist = f"solidworks_mcp-{version}.dist-info"
    out = os.path.join(ROOT, f"solidworks_mcp-{version}-py3-none-any.whl")
    files = []
    for folder, _, names in os.walk(PKG):
        if "__pycache__" in folder:
            continue
        for name in sorted(names):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = os.path.join(folder, name)
            files.append((os.path.relpath(path, SRC).replace(os.sep, "/"), open(path, "rb").read()))
    import glob
    found = glob.glob(os.path.join(SRC, "*.dist-info", "licenses", "LICENSE")) + \
        glob.glob(os.path.join(SRC, "*.dist-info", "LICENSE")) + glob.glob(os.path.join(ROOT, "LICENSE"))
    license_path = found[0] if found else None
    meta = [(f"{dist}/METADATA", METADATA.format(version=version).encode()),
            (f"{dist}/WHEEL", WHEEL.encode()),
            (f"{dist}/entry_points.txt", ENTRY.encode()),
            (f"{dist}/top_level.txt", b"solidworks_mcp\n")]
    if license_path:
        meta.append((f"{dist}/licenses/LICENSE", open(license_path, "rb").read()))
    record = [f"{name},{digest(data)},{len(data)}" for name, data in files + meta] + [f"{dist}/RECORD,,"]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as whl:
        for name, data in files + meta:
            whl.writestr(name, data)
        whl.writestr(f"{dist}/RECORD", "\n".join(record) + "\n")
    print(f"built {out} ({len(files)} package files)")
    return out


if __name__ == "__main__":
    main()
