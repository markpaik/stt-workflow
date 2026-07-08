"""refine_turns: the two-pass smoothing loop must not double-count a flag when
it revisits an unchanged short turn."""
import numpy as np

from stt import refine


def test_short_low_confidence_flag_not_doubled_across_two_passes(sandbox):
    """A short turn sandwiched by one unchanged speaker, with no voice evidence
    favoring the neighbour, hits the else branch on BOTH passes; the flag and
    stats['flagged'] must each count it once."""
    turns = [{"start": 0.0, "end": 5.0, "cluster": "SPEAKER_00"},
             {"start": 5.0, "end": 5.25, "cluster": "SPEAKER_01"},  # < 0.3s short
             {"start": 5.25, "end": 10.0, "cluster": "SPEAKER_00"}]
    # middle turn's embedding is usable but there are no voiceprints/centroids,
    # so neither own nor target reference exists -> else branch (flag it, keep)
    tembs = [None, np.ones(4, dtype=float), None]
    cluster_names = {"SPEAKER_00": None, "SPEAKER_01": None}

    merged, stats = refine.refine_turns(
        turns, tembs, cluster_names, voiceprints={}, strict=False)

    assert stats["flagged"] == 1
    flags = [s["flag"] for s in stats["spans"]]
    assert flags.count("short_low_confidence") == 1
