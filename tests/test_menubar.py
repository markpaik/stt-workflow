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


def _busy_port():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_a_panel_port_in_use_leaves_the_menu_bar_running(sandbox, monkeypatch):
    """start_server raised OSError out of STTMenuBar.__init__ when the port
    was taken (a second app, or a headless panel left running). launchd
    restarts a crashed menu bar, into the same error. start_panel returns
    the reason instead, and no relabel worker starts for a panel that
    cannot serve."""
    from gui import server as panel
    sock, port = _busy_port()
    try:
        monkeypatch.setattr(panel, "PORT", port)
        monkeypatch.setattr(panel, "RELABEL_QUEUE", None)
        why = menubar.start_panel()
        assert why and f"port {port} is in use" in why
        assert panel.RELABEL_QUEUE is None
    finally:
        sock.close()


def test_the_headless_panel_says_its_port_is_in_use_in_one_line(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    sock, port = _busy_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("STT_")}
    env.update(STT_HOME=str(tmp_path / "home"), STT_PANEL_PORT=str(port),
               STT_OLLAMA_URL="off", PYTHONPATH=str(repo))
    try:
        r = subprocess.run([sys.executable, "-m", "gui.server"], capture_output=True,
                           text=True, env=env, cwd=str(repo), timeout=60)
    finally:
        sock.close()
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert f"not started: port {port} is in use" in r.stderr
    assert "STT_PANEL_PORT" in r.stderr
