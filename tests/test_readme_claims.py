"""The README is published with the repo, so the claims it makes about the
tools must match the code. These tests pin the ones a code change can
silently break: the classic page is gone, every in-page link lands on a
heading, every tool it names exists, the registry_doctor table lists the
finding kinds the code reports, and the numbers it gives for the date
editor and the enroll safeguards are the code's own defaults."""
import json
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


def test_the_readme_says_the_mlx_fallback_is_optional():
    """The MLX fallback (.venv-llm) is optional and not installed by
    default. No line may still claim a smaller Mac or init.sh uses it."""
    assert "**Optional: the MLX fallback.** It is not installed by default" in README
    assert "Smaller Macs use the 8B MLX" not in README
    assert "and the local 8B model (about 4.5 GB)" not in README


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


def test_the_keep_alive_settings_and_defaults_are_the_codes():
    """The README table and stt.env.example list each keep_alive setting
    with the default summarize.py uses."""
    table = _section("The local model")
    example = (REPO / "stt.env.example").read_text(encoding="utf-8")
    assert len(summarize.KEEP_ALIVE_DEFAULTS) == 3
    for name, default in summarize.KEEP_ALIVE_DEFAULTS.items():
        assert f"| `{name}` | `{default}` |" in table, name
        assert f"\n# {name}={default}\n" in example, name


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


def test_no_published_file_installs_or_offers_the_removed_parakeet_engine():
    """Parakeet is gone, and the punctuator that cleaned up its text with it.
    The install no longer pulls parakeet-mlx or punctuators, the example
    settings and the docs no longer offer either, and the README names
    Parakeet only to say it was removed. NOTICE.md keeps its attribution for
    the transcripts it wrote."""
    for name in ("requirements.txt", "requirements-dev.txt", "requirements-qwen.txt",
                 "init.sh", "setup.sh", "run.sh", "stt.env.example", "gui/DESIGN.md"):
        text = (REPO / name).read_text(encoding="utf-8").lower()
        assert "parakeet" not in text and "punctuat" not in text, name
    assert not (REPO / "stt" / "asr_parakeet.py").exists()
    assert not (REPO / "stt" / "punctuate.py").exists()
    lines = [ln for ln in README.splitlines() if "parakeet" in ln.lower()]
    assert lines and all("the removed Parakeet engine" in ln for ln in lines), lines
    assert "STT_PUNCTUATE" not in README
    notice = (REPO / "NOTICE.md").read_text(encoding="utf-8")
    assert "no longer uses" in notice and "parakeet-mlx" not in notice


def test_the_shortcut_steps_match_the_v2_build():
    """The README builds the v2 delete Shortcut the way the owner's working
    build does: Get Dictionary from Input, Get Dictionary Value for title,
    min and max, Find Recordings by name and by two strict Duration rows in
    seconds ("is between" does not work), each value picked with Select
    Variable, Delete Recordings fed the Find result as a variable and never
    a memo picked from the list, and the count as the output. The numbers
    are the code's own, the hand test passes valid JSON, and the examples
    use synthetic titles."""
    from stt import voicememos
    section = _section("Removing memos from Voice Memos")
    text = " ".join(re.sub(r"(?m)^\s*>\s?", "", section).split())
    for step in ("**Stop and Respond**",
                 "Add **Get Dictionary from Input**, with **Shortcut Input** as its input",
                 "Add **Get Dictionary Value** three times",
                 "The keys are `title`, `min` and `max`, one per action",
                 "Set it to **All** of the following are true",
                 "**Recording Name** **is** the `title` value",
                 "**Duration** **is greater than** the `min` value, in **seconds**",
                 "**Duration** **is less than** the `max` value, in **seconds**",
                 "**Duration** **is between** does not work in this action",
                 "Their limits are strict",
                 "**Pick each value with Select Variable, never by typing.**",
                 "choose **Select Variable**, then tap the **Get Dictionary Value** block",
                 "**Delete Recordings** (Voice Memos), with the **Recordings** variable",
                 "**Pick the variable, never a memo.**", "the Voice Memos icon",
                 "**Stop and Output** with the **Count** variable",
                 "Find Recordings also finds memos in Recently Deleted",
                 "`shortcuts run` prints the count on standard output"):
        assert step in text, step
    assert "Get Text from Input" not in text
    assert "STT delete test" in text
    # the window and the twin limit are the code's
    assert f"a window of {voicememos.MATCH_WINDOW_SEC:g} seconds on each side" in text
    assert f"a length within {voicememos.TWIN_SLACK_SEC:g} seconds" in text
    assert f"`{voicememos.SHORTCUT_DEFAULT}`" in text
    assert (f"| `STT_VM_DELETE_SHORTCUT` | `{voicememos.SHORTCUT_DEFAULT}` |"
            in README)
    # the hand test: valid JSON with the three keys and a synthetic title,
    # and the count read from the terminal, never from an output file
    runs = re.findall(r"^\s*shortcuts run .*$", section, re.M)
    assert len(runs) == 2 and not any("--output-path" in r for r in runs), runs
    inputs = [json.loads(m) for m in re.findall(r"printf '(\{.*?\})'", section)]
    assert len(inputs) == 2
    for d in inputs:
        assert set(d) == {"title", "min", "max"}
        assert d["title"].startswith("STT delete test")
        assert isinstance(d["min"], (int, float)) and d["min"] < d["max"]
    example = json.loads(re.search(r"```json\n(.*?)\n```", section, re.S).group(1))
    assert example == voicememos._match_input("Budget Review", 1200.0)


def test_the_readme_documents_the_mirror_and_its_doctor_kinds():
    """The transcript mirror sends text off the Mac through a sync program,
    so the README's 'What leaves your Mac' table must say so, and the
    doctor kinds it names must be the code's."""
    from stt import mirror
    src = Path(mirror.__file__).read_text(encoding="utf-8")
    kinds = set(re.findall(r'\("(mirror_[a-z]+)"', src))
    assert kinds == {"mirror_unavailable", "mirror_changed", "mirror_blocked"}
    assert set(re.findall(r"`(mirror_[a-z]+)`", README)) == kinds
    leaves = README.split("## What leaves your Mac", 1)[1].split("\n## ", 1)[0]
    assert "`STT_MIRROR_DIR`" in leaves
    assert ("The mirror does not copy a strict transcript unless you set "
            "`STT_MIRROR_STRICT=1`.") in " ".join(leaves.split())
    assert "| `STT_MIRROR_DIR` | empty (off) |" in README
    assert "| `STT_MIRROR_STRICT` | `0` |" in README
    assert f'"{mirror.STRICT_SETTING}"' == '"STT_MIRROR_STRICT"'
    example = (REPO / "stt.env.example").read_text(encoding="utf-8")
    assert "# STT_MIRROR_DIR=" in example and "\n# STT_MIRROR_STRICT=1\n" in example
