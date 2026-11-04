"""Install the pinned upstream SenseVoice ONNX bundle; never upload audio."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
SHA256 = "7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e"
FILES = {"model.int8.onnx", "tokens.txt", "LICENSE", "README.md",
         *(f"test_wavs/{lang}.wav" for lang in ("zh", "en", "ja", "ko", "yue"))}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def install(target: Path, archive: Path | None = None):
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".asr-install-", dir=target.parent) as td:
        if archive is None:
            archive = Path(td) / "model.tar.bz2"
            print("Downloading the pinned 155 MiB SenseVoice int8 bundle…", flush=True)
            req = urllib.request.Request(URL, headers={"User-Agent": "SophosChat-ASR-Setup/1"})
            with urllib.request.urlopen(req, timeout=60) as source, archive.open("wb") as out:
                total = 0
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > 200 * 1024 * 1024:
                        raise ValueError("model_archive_too_large")
                    out.write(chunk)
        if digest(archive) != SHA256:
            raise ValueError("model_archive_checksum_mismatch")
        stage = Path(td) / "files"
        stage.mkdir()
        with tarfile.open(archive, "r:bz2") as tar:
            for member in tar:
                rel = member.name.partition("/")[2]
                if rel not in FILES or not member.isfile():
                    continue
                dest = stage / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
        if not all((stage / name).is_file() for name in FILES):
            raise ValueError("model_archive_incomplete")
        manifest = {"source": URL, "archive_sha256": SHA256,
                    "files": {name: digest(stage / name) for name in sorted(FILES)}}
        target.mkdir(parents=True, exist_ok=True)
        for name in sorted(FILES):
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / name, target / name)
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Installed local ASR model: {target}")


def main():
    p = argparse.ArgumentParser(description="Install the server-local SenseVoiceSmall int8 model")
    p.add_argument("--model-dir", type=Path, default=Path(os.environ.get("AICHAT_ASR_MODEL_DIR", str(Path(os.environ.get("AICHAT_DATA_DIR", "data")) / "models/sensevoice-small-int8"))))
    p.add_argument("--archive", type=Path, help="Use an already downloaded official bundle (checksum is still required)")
    args = p.parse_args()
    install(args.model_dir, args.archive)


if __name__ == "__main__":
    main()
