"""audio: a source ffmpeg cannot read fails with ffmpeg's own reason in one
short line, never the raw command line with absolute paths."""
import os
import shutil
import subprocess

import pytest

from stt import audio

pytestmark = pytest.mark.skipif(
    not (shutil.which(audio.FFMPEG) or os.path.exists(audio.FFMPEG)),
    reason="ffmpeg not installed")


@pytest.mark.parametrize("name, data, why", [
    ("Corrupt Clip.wav", b"garbage", "Invalid data found when processing input"),
    ("corrupt.m4a", b"hello\n", "moov atom not found"),
])
def test_an_unreadable_source_says_why(tmp_path, name, data, why):
    src = tmp_path / name
    src.write_bytes(data)
    with pytest.raises(audio.UnreadableAudio) as ei:
        audio.to_wav16k(src, tmp_path / "out.wav")
    msg = str(ei.value)
    assert msg == f"ffmpeg could not decode {name}: {why}"
    assert str(tmp_path) not in msg          # no absolute path
    assert "exit status" not in msg and "-ar" not in msg   # no command line
    assert isinstance(ei.value, RuntimeError)


def test_the_reason_drops_paths_and_ffmpeg_prefixes(tmp_path):
    src = tmp_path / "x y.m4a"
    err = (f"[in#0 @ 0x7787024000] moov atom not found\n"
           f"[in#0 @ 0x7787020000] Error opening input: Invalid data found when processing input\n"
           f"Error opening input file {src}.\n"
           f"Error opening input files: Invalid data found when processing input\n")
    assert audio._ffmpeg_reason(err, src) == "moov atom not found"
    assert audio._ffmpeg_reason(f"{src}: Permission denied\n", src) == "x y.m4a: Permission denied"
    assert audio._ffmpeg_reason("", src) == "unknown error"
    assert audio._ffmpeg_reason(b"\xff\xfe broken \n", src) == "broken"


def test_a_good_source_still_decodes(tmp_path):
    src = tmp_path / "tone.m4a"
    subprocess.run([audio.FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", str(src)], check=True)
    out = audio.to_wav16k(src, tmp_path / "out.wav")
    assert out.stat().st_size > 1000
