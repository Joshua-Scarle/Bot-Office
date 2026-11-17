"""The voice installer and lock file agree with what the controller expects."""

import re
import subprocess
from pathlib import Path

from botoffice.config import STT_MODELS
from botoffice.voice import KOKORO_FILES, KOKORO_VOICES

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_voice.sh"
LOCK = ROOT / "deploy" / "voice-requirements.txt"


def test_lock_pins_every_package_with_hashes():
    entries = re.split(r"\n(?=[a-z])", LOCK.read_text().split("\n\n", 1)[1].strip())
    names = set()
    for entry in entries:
        assert re.match(r"^[a-z0-9-]+==[\w.]+", entry), entry
        assert "--hash=sha256:" in entry, entry
        names.add(entry.split("==")[0])
    assert {"faster-whisper", "kokoro-onnx", "onnxruntime", "ctranslate2", "numpy"} <= names
    assert "torch" not in names


def test_installer_knows_every_configured_model():
    script = SCRIPT.read_text()
    for model in STT_MODELS:
        assert f"[{model}]=" in script
    for name in (*KOKORO_FILES.values(), KOKORO_VOICES):
        assert f"[{name}]=" in script
    assert "--require-hashes --only-binary=:all:" in script
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0


def test_uninstall_removes_only_the_voice_dir(tmp_path):
    (tmp_path / "voice" / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "voice" / "venv" / "bin" / "python").write_text("")
    (tmp_path / "voice" / "venv" / "bin" / "python").chmod(0o755)
    (tmp_path / "bots").mkdir()
    result = subprocess.run(["bash", str(SCRIPT), "--uninstall", "--data-dir", str(tmp_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "voice").exists() and (tmp_path / "bots").is_dir()


def test_bad_options_are_rejected(tmp_path):
    for args in (["--stt-model", "large-v3"], ["--tts-model", "fp16"], ["--bogus"]):
        result = subprocess.run(["bash", str(SCRIPT), "--data-dir", str(tmp_path), *args],
                                capture_output=True, text=True, check=False)
        assert result.returncode == 2
    assert not (tmp_path / "voice").exists()


def test_installer_voice_allowlist_matches_the_picker():
    from botoffice.voice import VOICES

    line = next(line for line in SCRIPT.read_text().splitlines() if "allowlist = {" in line)
    assert set(re.findall(r'"(\w+)"', line)) == set(VOICES)


def test_uninstall_refuses_a_directory_that_is_not_a_voice_install(tmp_path):
    (tmp_path / "voice" / "precious").mkdir(parents=True)
    result = subprocess.run(["bash", str(SCRIPT), "--uninstall", "--data-dir", str(tmp_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1 and "not a voice install" in result.stderr
    assert (tmp_path / "voice" / "precious").is_dir()
    (tmp_path / "voice" / "MANIFEST.json").write_text("{}")
    result = subprocess.run(["bash", str(SCRIPT), "--uninstall", "--data-dir", str(tmp_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0 and not (tmp_path / "voice").exists()


def test_root_or_home_is_never_used_as_the_data_dir(tmp_path):
    import os

    home = tmp_path / "home"
    (home / "voice" / "venv" / "bin").mkdir(parents=True)
    (home / "voice" / "MANIFEST.json").write_text("{}")
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    mode = home.stat().st_mode
    for data_dir in ("/", str(home), str(home) + "/", "~", ""):
        for extra in (["--uninstall"], []):
            result = subprocess.run(["bash", str(SCRIPT), *extra, "--data-dir", data_dir],
                                    capture_output=True, text=True, check=False, env=env)
            assert result.returncode == 2 and "refusing" in result.stderr, (data_dir, extra)
    assert (home / "voice" / "MANIFEST.json").exists()
    assert home.stat().st_mode == mode  # never chmodded


def test_downloads_use_a_private_dir_size_cap_and_both_hashes(tmp_path):
    """Offline run with stub curl/venv: a planted .part symlink is never written through,
    curl gets --max-filesize, and a small file whose sha256 differs is refused."""
    import hashlib
    import os
    import sys

    files = tmp_path / "remote"
    files.mkdir()
    blobs = {"config.json": b'{"a": 1}', "tokenizer.json": b"tok", "vocabulary.txt": b"voc", "model.bin": b"M" * 64}

    def gitblob(data):
        return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

    for name, data in blobs.items():
        (files / name).write_bytes(data)
    script = SCRIPT.read_text()
    script = re.sub(r'\[small\.en\]="2657 gitblob \w+ \w+"',
                    f'[small.en]="{len(blobs["config.json"])} gitblob {gitblob(blobs["config.json"])} '
                    f'{hashlib.sha256(blobs["config.json"]).hexdigest()}"', script)
    script = re.sub(r'TOKENIZER="[^"]+"', f'TOKENIZER="3 gitblob {gitblob(b"tok")} {hashlib.sha256(b"tok").hexdigest()}"', script)
    # right blob id, wrong pinned sha256: must be refused
    script = re.sub(r'VOCABULARY="[^"]+"', f'VOCABULARY="3 gitblob {gitblob(b"voc")} {"0" * 64}"', script)
    script = re.sub(r'\[small\.en\]="483545366 sha256 \w+"',
                    f'[small.en]="64 sha256 {hashlib.sha256(blobs["model.bin"]).hexdigest()}"', script)
    copy = tmp_path / "scripts" / "install_voice.sh"
    copy.parent.mkdir()
    copy.write_text(script)
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "voice-requirements.txt").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "curl.log"
    (bin_dir / "curl").write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> {log}\n'
        'out=""; for ((i=1;i<=$#;i++)); do [[ "${!i}" == -o ]] && { j=$((i+1)); out="${!j}"; }; done\n'
        f'cp "{files}/${{@: -1}}" "$out" 2>/dev/null || cp "{files}/$(basename "${{@: -1}}")" "$out"\n')
    (bin_dir / "curl").chmod(0o755)
    stub_python = tmp_path / "py"
    stub_python.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == -m && "$2" == venv ]]; then mkdir -p "$3/bin";'
        f' printf \'#!/bin/bash\\n[[ "$2" == pip ]] && exit 0\\nexec {sys.executable} "$@"\\n\' > "$3/bin/python";'
        ' chmod +x "$3/bin/python"; exit 0; fi\n'
        f'exec {sys.executable} "$@"\n')
    stub_python.chmod(0o755)
    data = tmp_path / "data"
    whisper = data / "voice" / "whisper" / "small.en"
    whisper.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (whisper / "config.json.part").symlink_to(victim)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path), "PYTHON": str(stub_python)}
    result = subprocess.run(["bash", str(copy), "--data-dir", str(data), "--no-tts"],
                            capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 1 and "vocabulary.txt" in result.stderr and "does not match the pinned" in result.stderr
    assert victim.read_text() == "keep"
    assert (whisper / "config.json").read_bytes() == blobs["config.json"]
    assert not (whisper / "vocabulary.txt").exists()
    assert not list((data / "voice").glob(".download.*"))  # cleaned up on exit
    calls = log.read_text().splitlines()
    assert all("--max-filesize" in call for call in calls) and "--max-filesize 3 " in calls[1]
