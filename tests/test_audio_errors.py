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


def _silent_video(path):
    subprocess.run([audio.FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=d=1:s=160x120", "-c:v", "mpeg4", str(path)], check=True)
    return path


def test_a_video_with_no_audio_track_says_so(tmp_path):
    src = _silent_video(tmp_path / "Silent video.mov")
    with pytest.raises(audio.UnreadableAudio) as ei:
        audio.to_wav16k(src, tmp_path / "out.wav")
    assert str(ei.value) == "no audio track in Silent video.mov"


def test_a_failed_decode_leaves_no_meeting_folder(sandbox):
    """process_file made the meeting folder before it decoded the source,
    so a file that could not be read left an empty folder behind."""
    from stt import config, pipeline
    src = _silent_video(sandbox / "source" / "Silent video.mov")
    with pytest.raises(audio.UnreadableAudio):
        pipeline.process_file(src, dest_dir=config.MEETINGS_DIR, do_diarize=False)
    assert list(config.MEETINGS_DIR.iterdir()) == []


def test_a_folder_ffmpeg_may_not_write_is_not_the_sources_fault(tmp_path):
    """Every non-zero exit was UnreadableAudio, so a read-only work folder
    set a good file aside at its first failure, and the message said
    "could not decode" with the work folder's absolute path in it."""
    src = tmp_path / "tone.m4a"
    subprocess.run([audio.FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", str(src)], check=True)
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        with pytest.raises(audio.WriteFailed) as ei:
            audio.to_wav16k(src, ro / "out.wav")
    finally:
        ro.chmod(0o755)
    assert not isinstance(ei.value, audio.UnreadableAudio)
    assert str(ei.value) == ("could not write the converted audio for tone.m4a: "
                             "Permission denied")


def test_a_full_disk_is_not_the_sources_fault(tmp_path, monkeypatch):
    # ffmpeg's own words when the disk filled during a decode
    full = ("[aost#0:0/pcm_s16le @ 0x79e90d0000] Error submitting a packet to "
            "the muxer: No space left on device\n"
            "[out#0/wav @ 0x79e904c180] Error muxing a packet\n"
            "[out#0/wav @ 0x79e904c180] Error writing trailer: No space left "
            "on device\n")
    monkeypatch.setattr(audio.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a, 228, b"", full.encode()))
    with pytest.raises(audio.WriteFailed) as ei:
        audio.to_wav16k(tmp_path / "Board.m4a", tmp_path / "w" / "out.wav")
    assert str(ei.value) == ("could not write the converted audio for Board.m4a: "
                             "No space left on device")
    # an input-side failure stays the source's
    bad = "[in#0 @ 0x77] Error opening input: Invalid data found when processing input\n"
    assert audio._output_side(bad) is None
