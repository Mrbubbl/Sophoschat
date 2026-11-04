"""Resident CPU recognizer; JSON lines in/out. No database or network access."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


class Recognizer:
    def __init__(self):
        self.model = None
        self.key = None

    def recognize(self, job):
        import numpy as np
        import sherpa_onnx

        limit = job["max_seconds"]
        try:
            audio = subprocess.run([job["ffmpeg"], "-nostdin", "-v", "error", "-threads", "1",
                "-protocol_whitelist", "file,pipe", "-i", job["path"], "-t", str(limit + 1),
                "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1"], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return {"error": "asr_invalid_audio"}
        if audio.returncode or not audio.stdout:
            return {"error": "asr_invalid_audio"}
        samples = np.frombuffer(audio.stdout, dtype="<f4").copy()
        if len(samples) > limit * 16000:
            return {"error": "asr_too_long"}
        if len(samples) < 800 or not np.isfinite(samples).all() or np.sqrt(np.mean(samples ** 2)) < 0.0005:
            return {"error": "asr_no_speech"}
        key = (job["model_dir"], job["threads"])
        if self.key != key:
            d = Path(job["model_dir"])
            self.model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(d / "model.int8.onnx"), tokens=str(d / "tokens.txt"),
                num_threads=job["threads"], provider="cpu", language="auto", use_itn=True)
            self.key = key
        parts = []
        start = 0
        while start < len(samples):
            end = min(start + 20 * 16000, len(samples))
            # Prefer a quiet 100 ms boundary in the last three seconds of a chunk.
            if end < len(samples):
                choices = range(end - 3 * 16000, end, 1600)
                end = min(choices, key=lambda i: float(np.mean(samples[i:i + 1600] ** 2))) + 1600
            piece = samples[start:end]
            start = end
            if len(piece) < 800 or np.sqrt(np.mean(piece ** 2)) < 0.0005:
                continue
            stream = self.model.create_stream()
            stream.accept_waveform(16000, piece)
            self.model.decode_stream(stream)
            text = re.sub(r"<\|[^>]*\|>", "", stream.result.text).strip()
            if text:
                parts.append(text)
        return {"text": " ".join(parts), "engine": "sensevoice-small-int8", "seconds": len(samples) / 16000}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--parent", type=int)
    args = p.parse_args()
    if args.parent:
        from aichat_agent.watchdog import watch_parent
        watch_parent(args.parent)
    recognizer = Recognizer()
    for line in sys.stdin:
        try:
            result = recognizer.recognize(json.loads(line))
        except Exception:
            result = {"error": "asr_failed"}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
