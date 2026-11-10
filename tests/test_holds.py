"""holds: the names parked in the queue. A damaged or hand-edited holds.json
must never read as different names, and must never be written over with
the other holds lost and no message."""
import pytest

from stt import holds


@pytest.mark.parametrize("text", ['"abc"', '{"a.wav": 1}', "123", "null"])
def test_a_holds_file_of_the_wrong_shape_holds_nothing(sandbox, text):
    """set() of a JSON string held 'a', 'b' and 'c', and of an object its
    keys."""
    holds.PATH.write_text(text)
    assert holds.items() == set()
    assert not holds.is_held("a") and not holds.is_held("a.wav")


def test_only_the_strings_in_the_list_are_names(sandbox):
    holds.PATH.write_text('["keep.wav", 1, ["nested"], null]')
    assert holds.items() == {"keep.wav"}
    assert holds.hold("new.wav") is True
    assert holds.items() == {"keep.wav", "new.wav"}


def test_a_truncated_holds_file_is_kept_aside_before_the_next_hold(sandbox, capsys):
    """The next hold() wrote over a truncated file, and every other hold was
    gone with no message."""
    torn = '["keep1.wav","keep2.wav"'
    holds.PATH.write_text(torn)
    assert holds.items() == set()
    holds.hold("new.wav")
    assert holds.items() == {"new.wav"}
    bad = holds.PATH.with_suffix(".json.bad")
    assert bad.read_text() == torn
    assert "holds.json.bad" in capsys.readouterr().err


def test_a_good_holds_file_is_never_set_aside(sandbox):
    holds.hold("a.wav")
    holds.toggle("b.wav")
    holds.release("a.wav")
    assert holds.items() == {"b.wav"}
    assert not holds.PATH.with_suffix(".json.bad").exists()


def test_the_doctor_reads_a_bad_holds_file_as_no_holds(sandbox):
    """The doctor read the file itself: a JSON number raised TypeError and a
    string was checked letter by letter."""
    from stt import doctor
    holds.PATH.write_text("123")
    assert doctor._stale_holds() == []
    holds.PATH.write_text('"ab"')
    assert doctor._stale_holds() == []
    holds.PATH.write_text('["gone.wav", 7]')
    assert doctor._stale_holds() == ["gone.wav"]
