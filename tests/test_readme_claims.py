"""The README is published with the repo, so the claims it makes about the
tools must match the code. These tests pin the ones a code change can
silently break: the classic page is gone, every in-page link lands on a
heading, every tool it names exists, the registry_doctor table lists the
finding kinds the code reports, and the numbers it gives for the date
editor and the enroll safeguards are the code's own defaults."""
import re
from pathlib import Path

from stt import config, registry_doctor, summarize

REPO = Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")


def _section(heading: str) -> str:
    """The text under one '### heading', up to the next heading."""
    m = re.search(rf"^### {re.escape(heading)}\n(.*?)(?=^#{{2,3}} )", README, re.S | re.M)
    assert m, heading
    return m.group(1)


def _anchor(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped,
    spaces as dashes."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def test_the_readme_never_mentions_the_classic_page():
    # the old page is deleted (/classic redirects to /), and the
    # screenshots no longer show its sidebar link
    assert "classic" not in README.lower()


def test_every_in_page_link_lands_on_a_heading():
    heads = {_anchor(h) for h in re.findall(r"^#{2,3} (.+)$", README, re.M)}
    links = set(re.findall(r"\]\(#([\w-]+)\)", README))
    assert links and links <= heads, links - heads


def test_every_tool_the_readme_names_exists():
    named = set(re.findall(r"tools/([\w]+\.py)", README))
    named |= set(re.findall(r"^\| `([\w]+\.py)`", README, re.M))
    for row in re.findall(r"^\| ((?:`[\w]+\.py`, )+`[\w]+\.py`) \|", README, re.M):
        named |= set(re.findall(r"`([\w]+\.py)`", row))
    missing = sorted(n for n in named if not (REPO / "tools" / n).exists())
    assert named and not missing, missing


def test_the_registry_table_lists_every_finding_kind_the_doctor_reports():
    src = Path(registry_doctor.__file__).read_text(encoding="utf-8")
    kinds = set(re.findall(r'Finding\("([a-z_]+)"', src))
    table = set(re.findall(r"^\| `([a-z_]+)` \|", _section("Repairing the voice registries"),
                           re.M))
    assert kinds and table == kinds


def test_the_date_editor_range_is_the_codes():
    text = " ".join(_section("Dates").split())
    assert f"between {summarize.DATE_YEAR_MIN} and the end of next year" in text


def _default(var: str) -> str:
    """The default stt/config.py gives `var`, read from its source, so a
    value set in the environment or stt.env cannot move the test."""
    src = Path(config.__file__).read_text(encoding="utf-8")
    m = (re.search(rf'os\.environ\.get\("{var}", "([^"]+)"\)', src)
         or re.search(rf'env_(?:float|int)\("{var}", (-?[\d.]+)\)', src))
    assert m, var
    return f"{float(m.group(1)):g}"


def test_the_enroll_numbers_are_the_code_defaults():
    text = " ".join(_section("Enrolling from the command line").split())
    assert f"less than {_default('STT_UNKNOWN_MIN_TALK_SECS')} seconds of speech" in text
    assert (f"fewer than {_default('STT_UNKNOWN_MIN_RELIABLE_TURNS')} turns of "
            f"{_default('STT_REFINE_MIN_DUR')} seconds or more") in text
    assert f"below {_default('STT_ENROLL_STACK_MIN')} against" in text


def test_every_screenshot_the_readme_shows_is_in_docs_img_and_none_is_stray():
    """The eight screenshots are retaken from a demo home whenever the
    panel changes. A renamed or dropped capture left a broken image on
    GitHub, and a stray file shipped a picture nobody reviewed. The sizes
    stay the ones the README's width attributes were set for."""
    import struct
    SIZES = {(2880, 1920), (2880, 2000), (780, 1688)}
    shown = set(re.findall(r'<img src="(docs/img/[\w-]+\.png)"', README))
    on_disk = {f"docs/img/{p.name}" for p in (REPO / "docs" / "img").glob("*.png")}
    assert shown and shown == on_disk, shown ^ on_disk
    for rel in sorted(shown):
        head = (REPO / rel).read_bytes()[:24]
        assert head[:8] == b"\x89PNG\r\n\x1a\n", rel
        # the capture sizes at 2x: 1440x960 and 1440x1000 desktop, 390x844 phone
        assert struct.unpack(">II", head[16:24]) in SIZES, rel


def test_no_published_file_installs_or_offers_the_removed_whisper_engine():
    """The local Whisper engine is gone. The install no longer pulls
    mlx-whisper, the example settings and the docs no longer offer it, and
    the README names "mlxwhisper" only to say it was removed. The OpenAI
    cloud engine's own model (whisper-1) is not the local engine."""
    for name in ("requirements.txt", "requirements-dev.txt", "init.sh", "setup.sh",
                 "stt.env.example", "gui/DESIGN.md"):
        text = (REPO / name).read_text(encoding="utf-8").lower()
        assert "mlx-whisper" not in text and "mlxwhisper" not in text, name
        assert "whisper_mlx" not in text and "whisperx" not in text, name
    assert not (REPO / "stt" / "asr_mlxwhisper.py").exists()
    lines = [ln for ln in README.splitlines() if "whisper" in ln.lower()]
    assert lines and all("the removed Whisper engine" in ln for ln in lines), lines
