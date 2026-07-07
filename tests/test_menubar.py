"""menubar: gather() must agree with the panel about what's queued (a Redo
waiting behind an in-flight batch), and never show a stage label the panel
already knows how to name."""
from gui import menubar
from stt import jobs


def test_stage_label_covers_verifying():
    assert "verifying" in menubar.STAGE_LABEL


def test_gather_includes_queued_panel_jobs(sandbox):
    jobs.add({"paths": ["/x/A.m4a"], "label": "A.m4a"})
    s = menubar.gather()
    assert len(s["queued_jobs"]) == 1
    assert s["queued_jobs"][0]["label"] == "A.m4a"


def test_gather_reports_no_queued_jobs_when_empty(sandbox):
    assert menubar.gather()["queued_jobs"] == []


def test_signature_changes_when_a_job_is_queued(sandbox):
    """The dropdown only re-renders when the signature changes — a queued job
    appearing/disappearing must be part of that signature, or the panel and
    menu bar can visibly disagree (panel shows a queued Redo, menu bar shows
    "Nothing waiting") until something ELSE happens to trigger a re-render."""
    s_before = menubar.gather()
    sig_before = menubar.STTMenuBar._signature(s_before)
    jobs.add({"paths": ["/x/A.m4a"], "label": "A.m4a"})
    s_after = menubar.gather()
    sig_after = menubar.STTMenuBar._signature(s_after)
    assert sig_before != sig_after


def test_change_schedule_dead_code_removed():
    """The schedule dialog now lives only in the web panel — this duplicate
    was unreachable (no menu item ever called it) and has been removed."""
    assert not hasattr(menubar.STTMenuBar, "change_schedule")
    assert not hasattr(menubar.STTMenuBar, "_write_schedule")
