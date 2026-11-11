"""Local speech-to-text + diarization pipeline for meeting recordings.

Qwen3-ASR 1.7B (via mlx-audio, in its own environment) for transcription +
speakrs (pyannote community-1 on Core ML; pyannote on CPU as fallback) for
diarization,
with a voiceprint layer that names recurring speakers. Fully offline after first
model download. See the plan at ~/.claude/plans/ for the full design.
"""
