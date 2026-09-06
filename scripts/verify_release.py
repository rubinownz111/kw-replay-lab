"""Verify archive integrity and run the extracted app independently of the source tree."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    archive_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist/kw-replay-lab-1.02.zip"
    result = {"archive": archive_path.name, "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest()}
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    with tempfile.TemporaryDirectory(prefix="kw-release-check-") as directory:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            assert len(names) == len(set(names)), "Duplicate archive members"
            for name in names:
                path = PurePosixPath(name)
                assert not path.is_absolute() and ".." not in path.parts and "\\" not in name and ":" not in name
                assert path.parts[0] == "kw-replay-lab"
            manifest = json.loads(archive.read("kw-replay-lab/release-manifest.json"))
            expected = {"kw-replay-lab/release-manifest.json"}
            for entry in manifest:
                name = "kw-replay-lab/" + entry["path"]
                data = archive.read(name)
                assert len(data) == entry["size"]
                assert hashlib.sha256(data).hexdigest() == entry["sha256"]
                expected.add(name)
            assert set(names) == expected
            archive.extractall(directory)
        extracted = Path(directory) / "kw-replay-lab"
        completed = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=extracted, env=env, capture_output=True, text=True)
        if completed.returncode:
            raise RuntimeError(completed.stdout + completed.stderr)
        result["unit_tests"] = completed.stderr.splitlines()[-4:]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with (Path(directory) / "server.log").open("w") as log:
            process = subprocess.Popen([sys.executable, "app.py", "--no-browser", "--port", str(port)], cwd=extracted, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                base = f"http://127.0.0.1:{port}"
                for attempt in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Extracted server exited before becoming ready")
                    try:
                        with urllib.request.urlopen(base + "/api/health", timeout=1) as response:
                            health = json.load(response)
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Extracted server did not become ready")
                assert health["supported_game_version"] == "1.2.0.0"
                assert health["report_schema_version"] == 7
                with urllib.request.urlopen(base + "/", timeout=10) as response:
                    assert response.status == 200 and b"KW Replay" in response.read()
                with urllib.request.urlopen(base + "/api/demo", timeout=10) as response:
                    report = json.load(response)
                assert report["evidence"]["sample_kind"] == "synthetic_not_engine_recorded"
                fixture = subprocess.check_output([sys.executable, "-c", "import sys; from replay_analyzer.demo import build_auxiliary_replay; sys.stdout.buffer.write(build_auxiliary_replay())"], cwd=extracted, env=env)
                boundary = "kw-release-validation-boundary"
                body = (f'--{boundary}\r\nContent-Disposition: form-data; name="replay"; filename="release.KWReplay"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + fixture + f"\r\n--{boundary}--\r\n".encode())
                request = urllib.request.Request(base + "/api/analyze", data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    uploaded = json.load(response)
                assert uploaded["commands"] == report["commands"]
                result.update(manifest_files=len(manifest), extracted_unit_tests_passed=True, waitress_health_passed=True, landing_passed=True, demo_passed=True, upload_passed=True)
            finally:
                process.terminate()
                process.wait(timeout=10)
    output = ROOT / ".artifacts/release-verification.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
