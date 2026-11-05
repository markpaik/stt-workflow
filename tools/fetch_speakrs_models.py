"""Download the speakrs Core ML models once, pinned and checksum-verified.

Usage: python3 tools/fetch_speakrs_models.py [dest_dir]

The default destination is config.SPEAKRS_MODELS. Files come from
huggingface.co/avencera/speakrs-models at config.SPEAKRS_MODELS_REV. Every
file's size is checked against the Hub listing, and every LFS file's SHA-256
too. Nothing is uploaded: this is the one-time model download the README
allows. Re-running skips files that are already present and verified.
"""
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from stt import config, speakrs_diar  # noqa: E402

REPO = "avencera/speakrs-models"


def wanted_files():
    # one list, shared with speakrs_diar.available(), so the readiness check
    # and the download can never disagree on what a complete bundle is
    return speakrs_diar.model_files()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(dest: Path, rev: str = config.SPEAKRS_MODELS_REV) -> int:
    api = f"https://huggingface.co/api/models/{REPO}/revision/{rev}?blobs=true"
    with urllib.request.urlopen(api, timeout=30) as r:
        sib = {x["rfilename"]: x for x in json.load(r)["siblings"]}
    missing = [f for f in wanted_files() if f not in sib]
    if missing:
        raise SystemExit(f"model listing lacks {len(missing)} file(s): {missing[:3]}")
    total = 0
    for f in wanted_files():
        dst = dest / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        want_sha = sib[f].get("lfs", {}).get("sha256")
        ok = (dst.exists() and dst.stat().st_size == sib[f]["size"]
              and (not want_sha or sha256(dst) == want_sha))
        if not ok:
            part = dst.with_name(dst.name + ".part")
            subprocess.run(["curl", "-sSLf", "-m", "900", "-o", str(part),
                            f"https://huggingface.co/{REPO}/resolve/{rev}/{f}"], check=True)
            if part.stat().st_size != sib[f]["size"]:
                raise SystemExit(f"size mismatch: {f}")
            if want_sha and sha256(part) != want_sha:
                raise SystemExit(f"checksum mismatch: {f}")
            os.replace(part, dst)
        total += dst.stat().st_size
    return total


if __name__ == "__main__":
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else config.SPEAKRS_MODELS
    n = fetch(dest)
    print(f"speakrs models ready: {len(wanted_files())} files, {n / 1e6:.0f} MB in {dest}")
