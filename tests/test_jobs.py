"""jobs: the panel's queued-run store — a Redo clicked mid-batch must wait
visibly, never vanish on the single-instance lock."""
from stt import jobs


def test_add_items_remove_roundtrip(sandbox):
    assert jobs.items() == []
    jobs.add({"paths": ["/x/A.m4a"], "force": True, "label": "A"})
    jobs.add({"files": ["B.m4a"], "strict": True, "label": "B"})
    got = jobs.items()
    assert [j["label"] for j in got] == ["A", "B"]
    assert all("at" in j for j in got)
    # claim (what run_batch does once it holds the lock)
    assert jobs.remove(got[0]["at"]) is True
    assert [j["label"] for j in jobs.items()] == ["B"]
    # double-claim is a no-op, not an error (lost lock race)
    assert jobs.remove(got[0]["at"]) is False


def test_spawn_args_carry_all_options(sandbox):
    jobs.add({"paths": ["/x/A.m4a"], "force": True, "strict": True,
              "verify": True, "parallel": 2, "label": "A"})
    j = jobs.items()[0]
    args = jobs.spawn_args(j)
    assert args[:3] == ["caffeinate", "-i", "-s"]
    for want in ("--ignore-pause", "--job", "--paths", "--force", "--strict",
                 "--verify", "--parallel"):
        assert want in args
    assert args[args.index("--job") + 1] == str(j["at"])


def test_corrupt_queue_degrades_to_empty(sandbox):
    jobs.PATH.write_text("{nope")
    assert jobs.items() == []
    jobs.add({"label": "ok"})  # and recovers on the next write
    assert len(jobs.items()) == 1
