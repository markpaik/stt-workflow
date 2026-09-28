"""stt.queue_order: the length of a queued file, and the order the queue runs
in (shortest first), shared by run_batch and the panel."""
import os

from stt import audio, config, icloud, queue_order


def _file(name, size=4096):
    p = config.source_dir() / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * size)
    return p


def test_a_local_file_is_probed_exactly_and_cached_by_path_and_mtime(sandbox, monkeypatch):
    p = _file("local.m4a")
    calls = []
    monkeypatch.setattr(icloud, "_fully_present", lambda q: True)
    monkeypatch.setattr(audio, "duration_sec", lambda q: calls.append(q) or 1800.0)
    assert queue_order.duration(p) == 1800.0
    assert queue_order.duration(p) == 1800.0
    assert len(calls) == 1, "a second read of the same file must not re-probe"
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 10))     # the file changed
    assert queue_order.duration(p) == 1800.0
    assert len(calls) == 2


def test_a_dataless_icloud_file_is_estimated_from_its_size_never_probed(
        sandbox, monkeypatch):
    """Probing a dataless placeholder forces its download."""
    audio_p = _file("cloud.m4a", size=30_000_000)       # ~30 min of voice memo
    video_p = _file("cloud.mp4", size=60_000_000)       # ~10 min of video
    monkeypatch.setattr(icloud, "_fully_present", lambda q: False)

    def never(q):
        raise AssertionError(f"probed a dataless file: {q}")
    monkeypatch.setattr(audio, "duration_sec", never)
    assert abs(queue_order.duration(audio_p) - 1800.0) < 1
    assert abs(queue_order.duration(video_p) - 600.0) < 1


def test_an_unreadable_or_missing_file_has_no_length(sandbox, monkeypatch):
    p = _file("broken.m4a")
    monkeypatch.setattr(icloud, "_fully_present", lambda q: True)

    def boom(q):
        raise RuntimeError("ffprobe failed")
    monkeypatch.setattr(audio, "duration_sec", boom)
    assert queue_order.duration(p) is None
    assert queue_order.duration(config.source_dir() / "gone.m4a") is None


def test_shortest_first_breaks_ties_by_name_and_puts_unknown_last(sandbox, monkeypatch):
    secs = {"b.m4a": 600.0, "a.m4a": 600.0, "long.m4a": 9000.0, "short.m4a": 60.0}
    monkeypatch.setattr(queue_order, "duration",
                        lambda p: secs.get(os.path.basename(str(p))))
    names = ["long.m4a", "unknown.m4a", "b.m4a", "short.m4a", "a.m4a"]
    got = [p.name for p in queue_order.shortest_first([config.source_dir() / n
                                                       for n in names])]
    assert got == ["short.m4a", "a.m4a", "b.m4a", "long.m4a", "unknown.m4a"]
