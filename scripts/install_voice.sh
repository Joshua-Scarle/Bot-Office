#!/usr/bin/env bash
# Optional local voice for Bot-Office: dictation (faster-whisper) and read-aloud (Kokoro-82M).
#
# Everything goes under <data dir>/voice/, never into the Bot-Office venv:
#   voice/venv/      a separate Python venv with the hash-locked engine packages
#   voice/whisper/   the speech-to-text model (pinned Hugging Face revision + sha256)
#   voice/kokoro/    the text-to-speech model and voicepack (pinned GitHub release + sha256)
#   voice/MANIFEST.json  what was installed, from where, and its sha256
#
# Usage: scripts/install_voice.sh [--data-dir DIR] [--stt-model tiny.en|base.en|small.en]
#                                 [--tts-model fp32|int8] [--no-stt] [--no-tts] [--restart]
#        scripts/install_voice.sh --uninstall [--data-dir DIR]
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
data_dir="${BOT_OFFICE_DATA_DIR:-${AGENTDESK_DATA_DIR:-$HOME/printpilot-agentdesk/data}}"
stt_model="${BOT_OFFICE_STT_MODEL:-small.en}"
tts_model="${BOT_OFFICE_TTS_MODEL:-fp32}"
python="${PYTHON:-python3}"
want_stt=1 want_tts=1 restart=0 uninstall=0

while (($#)); do
  case "$1" in
    --data-dir) data_dir="$2"; shift 2 ;;
    --stt-model) stt_model="$2"; shift 2 ;;
    --tts-model) tts_model="$2"; shift 2 ;;
    --no-stt) want_stt=0; shift ;;
    --no-tts) want_tts=0; shift ;;
    --restart) restart=1; shift ;;
    --uninstall) uninstall=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

data_dir="${data_dir/#\~/$HOME}"
# never chmod or delete inside "/" or a home directory by mistake
resolved="$(realpath -m -- "${data_dir:-/}")"
if [[ -z "$data_dir" || "$resolved" == / || "$resolved" == "$(realpath -m -- "$HOME")" ]]; then
  echo "refusing to use '$data_dir' as the data dir; pass --data-dir with Bot-Office's data directory" >&2
  exit 2
fi
voice_dir="$data_dir/voice"
venv="$voice_dir/venv"
manifest="$voice_dir/MANIFEST.json"

restart_service() {
  if ((restart)) && systemctl --user cat bot-office.service >/dev/null 2>&1; then
    systemctl --user restart bot-office.service && echo "restarted bot-office.service"
  elif ((restart)); then
    echo "bot-office.service not found; restart Bot-Office yourself"
  else
    echo "Bot-Office notices the change within 5 minutes (or restart it now)."
  fi
}

if ((uninstall)); then
  if [[ -d "$voice_dir" && ! -L "$voice_dir" ]]; then
    if [[ ! -f "$voice_dir/MANIFEST.json" && ! -x "$voice_dir/venv/bin/python" ]]; then
      echo "$voice_dir has no MANIFEST.json or venv; not a voice install, not removing it" >&2
      exit 1
    fi
    rm -rf -- "$voice_dir"
    echo "removed $voice_dir"
  fi
  restart_service
  exit 0
fi

# --- pinned sources -------------------------------------------------------------
# faster-whisper models: Systran/faster-whisper-<model> at a fixed revision. model.bin is
# checked against its published LFS sha256; the small files against their git blob ids and
# the sha256 recorded when those blobs were first fetched (SIZE gitblob BLOB SHA256).
declare -A WHISPER_REV=(
  [tiny.en]=0d3d19a32d3338f10357c0889762bd8d64bbdeba
  [base.en]=3d3d5dee26484f91867d81cb899cfcf72b96be6c
  [small.en]=d1d751a5f8271d482d14ca55d9e2deeebbae577f
)
declare -A WHISPER_BIN=(
  [tiny.en]="75537502 sha256 1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a"
  [base.en]="145216508 sha256 2a166925539a16005f14ff328359f9b9adb9dc4fb631bb3b227526862e93e2ef"
  [small.en]="483545366 sha256 62b2a45b05ee59acb4a5341b33ee35e041395d378d418a18acfe4c9e768ee37a"
)
declare -A WHISPER_CONFIG=(
  [tiny.en]="2317 gitblob 4065bb3bed375b176d5465be117d2d202e210434 14b1b421a90349bc551b881461426b561a874049cb9e4c4864f2ca384f6a7cc5"
  [base.en]="2227 gitblob 594369787efe617005d199b03739ee0ead7e3ab7 f3bc3821e9fc76a27bae538e11ae5b677dcdd352b4600429ce7951d398569aeb"
  [small.en]="2657 gitblob ced3fd5bbb3e9f7664658065d2d36164712cf1d5 666a9605530ac1f61fa8177f3702b4dacec9966749e42610839fcc32661d5fae"
)
TOKENIZER="2128466 gitblob 15d7bdf9ba25718ca2504eec6a8f02bc55af0a6a 929c5252409436dce1b38a75d1abbcb5e132d170d8e324e4e04ed915fa2d22df"
VOCABULARY="422309 gitblob ee695b8d3e3c10d488304e04468efec4ca27554a ff77588746d3a2595d32ab5b69ffd7b95ce2441ac57533cb66fc3eb575a115cf"
# Kokoro-82M v1.0 ONNX export from the kokoro-onnx project's model-files-v1.1 release,
# checked against the sha256 digests GitHub publishes for the release assets.
KOKORO_URL=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1
declare -A KOKORO_FILE=(
  [kokoro-v1.0.onnx]="325505369 sha256 beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a"
  [kokoro-v1.0.int8.onnx]="114119327 sha256 ae315a79b623f244700e4afb9246c46a26066782e049ba174bf3ba433970ee9c"
  [voices-v1.0.bin]="28214398 sha256 bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"
)

case "$stt_model" in tiny.en|base.en|small.en) ;; *) echo "--stt-model must be tiny.en, base.en or small.en" >&2; exit 2 ;; esac
case "$tts_model" in fp32) kokoro_file=kokoro-v1.0.onnx ;; int8) kokoro_file=kokoro-v1.0.int8.onnx ;;
  *) echo "--tts-model must be fp32 or int8" >&2; exit 2 ;; esac

# --- checks -----------------------------------------------------------------------
if [[ "$(uname -s)/$(uname -m)" != "Linux/x86_64" ]]; then
  echo "the locked voice packages are for Linux x86_64 only (found $(uname -s)/$(uname -m))" >&2
  exit 1
fi
pyver="$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$pyver" in 3.11|3.12|3.13) ;; *) echo "need Python 3.11-3.13 for the voice venv (found $pyver); set PYTHON=" >&2; exit 1 ;; esac
command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }

umask 077
mkdir -p "$voice_dir"
chmod 700 "$data_dir" "$voice_dir"

# --- the separate voice venv ----------------------------------------------------------
if [[ ! -x "$venv/bin/python" ]]; then
  "$python" -m venv "$venv"
fi
"$venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-input \
  --require-hashes --only-binary=:all: --no-deps -r "$project_dir/deploy/voice-requirements.txt"

# --- downloads -------------------------------------------------------------------------
# verify FILE SIZE KIND HASH [SHA256]: KIND is sha256 or gitblob (git blob sha1, plus the
# pinned sha256 when given); prints the sha256
verify() {
  "$venv/bin/python" -I - "$@" <<'PY'
import hashlib, os, sys
path, size, kind, expected = sys.argv[1:5]
pinned = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None
actual_size = os.path.getsize(path)
if actual_size != int(size):
    sys.exit(f"{path}: size {actual_size}, expected {size}")
sha256, blob = hashlib.sha256(), hashlib.sha1(b"blob %d\0" % actual_size)
with open(path, "rb") as handle:
    for chunk in iter(lambda: handle.read(1 << 20), b""):
        sha256.update(chunk); blob.update(chunk)
digest = sha256.hexdigest()
if kind == "sha256" and digest != expected:
    sys.exit(f"{path}: sha256 {digest} does not match the pinned {expected}")
if kind == "gitblob" and blob.hexdigest() != expected:
    sys.exit(f"{path}: git blob {blob.hexdigest()} does not match the pinned {expected}")
if pinned and digest != pinned:
    sys.exit(f"{path}: sha256 {digest} does not match the pinned {pinned}")
print(digest)
PY
}

record() {  # record RELPATH SHA256 URL
  "$venv/bin/python" -I - "$manifest" "$@" <<'PY'
import json, os, sys, time
manifest, rel, digest, url = sys.argv[1:5]
data = json.load(open(manifest)) if os.path.exists(manifest) else {"files": {}}
data["files"][rel] = {"sha256": digest, "url": url,
                      "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
tmp = manifest + ".tmp"
with open(tmp, "w") as handle:
    json.dump(data, handle, indent=2, sort_keys=True)
os.chmod(tmp, 0o600)
os.replace(tmp, manifest)
PY
}

# downloads go to a fresh private directory (no predictable or pre-planted paths)
download_dir="$(mktemp -d "$voice_dir/.download.XXXXXX")"
trap 'rm -rf -- "$download_dir"' EXIT

fetch() {  # fetch URL RELPATH SIZE KIND HASH [SHA256]
  local url="$1" rel="$2" size="$3" kind="$4" hash="$5" sha="${6:-}" target="$voice_dir/$2" digest part
  mkdir -p "$(dirname "$target")"
  if [[ -f "$target" && ! -L "$target" ]] && digest="$(verify "$target" "$size" "$kind" "$hash" "$sha" 2>/dev/null)"; then
    echo "ok       $rel"
  else
    echo "download $rel ($((size / 1048576)) MiB)"
    part="$download_dir/${rel//\//_}"
    rm -f -- "$part"
    curl -fL --retry 3 --proto '=https' --proto-redir '=https' --tlsv1.2 --max-filesize "$size" -o "$part" "$url"
    digest="$(verify "$part" "$size" "$kind" "$hash" "$sha")" || { rm -f -- "$part"; exit 1; }
    rm -f -- "$target"
    mv -f -- "$part" "$target"
  fi
  chmod 600 "$target"
  record "$rel" "$digest" "$url"
}

if ((want_stt)); then
  rev="${WHISPER_REV[$stt_model]}"
  base="https://huggingface.co/Systran/faster-whisper-$stt_model/resolve/$rev"
  rel="whisper/$stt_model"
  read -r size kind hash sha <<<"${WHISPER_CONFIG[$stt_model]}"; fetch "$base/config.json" "$rel/config.json" "$size" "$kind" "$hash" "$sha"
  read -r size kind hash sha <<<"$TOKENIZER"; fetch "$base/tokenizer.json" "$rel/tokenizer.json" "$size" "$kind" "$hash" "$sha"
  read -r size kind hash sha <<<"$VOCABULARY"; fetch "$base/vocabulary.txt" "$rel/vocabulary.txt" "$size" "$kind" "$hash" "$sha"
  read -r size kind hash <<<"${WHISPER_BIN[$stt_model]}"; fetch "$base/model.bin" "$rel/model.bin" "$size" "$kind" "$hash"
fi
if ((want_tts)); then
  for file in "$kokoro_file" voices-v1.0.bin; do
    read -r size kind hash <<<"${KOKORO_FILE[$file]}"; fetch "$KOKORO_URL/$file" "kokoro/$file" "$size" "$kind" "$hash"
  done
fi
find "$voice_dir" -path "$venv" -prune -o -type d -exec chmod 700 {} +

# --- smoke test: load each engine once, the way the worker does ---------------------------
smoke() {
  env -i PATH=/usr/bin:/bin HOME="$HOME" HF_HUB_OFFLINE=1 "$venv/bin/python" -I - "$@" <<'PY'
import sys, time
kind = sys.argv[1]
started = time.monotonic()
if kind == "stt":
    import numpy as np
    from faster_whisper import WhisperModel
    model = WhisperModel(sys.argv[2], device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True)
    segments, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="en", beam_size=1)
    list(segments)
else:
    import zipfile
    from kokoro_onnx import Kokoro
    allowlist = {"bm_george", "bm_fable", "bm_lewis", "bm_daniel", "bf_emma", "am_michael", "af_heart"}
    packed = {name.removesuffix(".npy") for name in zipfile.ZipFile(sys.argv[3]).namelist()}
    if allowlist - packed:
        print("voices missing from the pack (hidden in the picker):", sorted(allowlist - packed))
    samples, rate = Kokoro(sys.argv[2], sys.argv[3]).create("Hello.", voice="bm_george", speed=1.0, lang="en-gb")
    assert len(samples) > rate // 10, "no audio produced"
print(f"{kind} smoke test passed in {time.monotonic() - started:.1f}s")
PY
}
((want_stt)) && smoke stt "$voice_dir/whisper/$stt_model"
((want_tts)) && smoke tts "$voice_dir/kokoro/$kokoro_file" "$voice_dir/kokoro/voices-v1.0.bin"

echo "voice installed in $voice_dir"
if [[ "$stt_model" != small.en ]]; then echo "set BOT_OFFICE_STT_MODEL=$stt_model for Bot-Office"; fi
if [[ "$tts_model" != fp32 ]]; then echo "set BOT_OFFICE_TTS_MODEL=$tts_model for Bot-Office"; fi
restart_service
