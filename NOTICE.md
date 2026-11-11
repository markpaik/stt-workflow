# Attribution

This pipeline uses the following open model, licensed **CC-BY-4.0**
(commercial use permitted **with attribution**):

- **pyannote speaker-diarization-community-1** — https://huggingface.co/pyannote/speaker-diarization-community-1

The Qwen3-ASR engine (`STT_ASR_BACKEND=qwen3`), the transcription engine,
uses two more open models, both licensed **Apache-2.0** (as their model
cards state):

- **Qwen3-ASR 1.7B** (MLX conversion of Qwen/Qwen3-ASR-1.7B): https://huggingface.co/mlx-community/Qwen3-ASR-1.7B-bf16
- **Qwen3-ForcedAligner 0.6B** (MLX 8-bit conversion of Qwen/Qwen3-ForcedAligner-0.6B): https://huggingface.co/mlx-community/Qwen3-ForcedAligner-0.6B-8bit

Runtimes / libraries: `mlx-audio` (MIT), `pyannote.audio` (MIT), MLX, PyTorch.

Transcripts made before 2026-11-11 were written by **NVIDIA Parakeet TDT
0.6B v3** (CC-BY-4.0, https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3),
which this pipeline no longer uses. Their meeting files still name it as
their engine, so keep its attribution with them if they are redistributed.

The control panel's UI typeface is **Figtree** by Erik Kennedy
(https://github.com/erikdkennedy/figtree), licensed under the **SIL Open Font
License 1.1**. The variable-font woff2 files are vendored in
`gui/static/fonts/` with the license text (`OFL.txt`) alongside, and the
panel serves them itself (`GET /static/fonts/...`), never from a CDN.

If transcripts or derived outputs are redistributed outside DPSCD, keep this
attribution with them.
