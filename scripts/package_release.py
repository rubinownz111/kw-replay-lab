"""Build a standalone source ZIP from an explicit, portable allowlist."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = {"app.py", "README.md", "CONTRIBUTING.md", "LICENSE", "THIRD-PARTY-NOTICES.md", "requirements.txt", "requirements-dev.txt", ".gitignore", ".gitattributes", "Start KW Replay Lab.bat", "package.json", "package-lock.json", "tsconfig.json"}
FOLDERS = {"replay_analyzer", "tools", "templates", "static/replay-lab", "frontend", "tests", "docs", "evidence", "scripts", ".github"}
SUFFIXES = {".py", ".json", ".md", ".html", ".css", ".js", ".mjs", ".cjs", ".ts", ".yml", ".txt"}


def selected_files():
    for name in sorted(FILES):
        path = ROOT / name
        if not path.is_file():
            raise RuntimeError(f"Missing release file: {name}")
        yield path
    for name in sorted(FOLDERS):
        for path in sorted((ROOT / name).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix in SUFFIXES:
                yield path


def main():
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    target = destination / "kw-replay-lab-1.02.zip"
    manifest = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in selected_files():
            data = path.read_bytes()
            name = path.relative_to(ROOT).as_posix()
            manifest.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            item = zipfile.ZipInfo("kw-replay-lab/" + name, date_time=(2026, 9, 6, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            archive.writestr(item, data)
        item = zipfile.ZipInfo("kw-replay-lab/release-manifest.json", date_time=(2026, 9, 6, 0, 0, 0))
        item.compress_type = zipfile.ZIP_DEFLATED
        item.external_attr = 0o644 << 16
        archive.writestr(item, json.dumps(manifest, indent=2))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (destination / "SHA256SUMS.txt").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(target), "files": len(manifest), "bytes": target.stat().st_size, "sha256": digest}, indent=2))


if __name__ == "__main__":
    main()
