# Attribution

This pipeline uses the following open models, both licensed **CC-BY-4.0**
(commercial use permitted **with attribution**):

- **NVIDIA Parakeet TDT 0.6B v2** — https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2
- **pyannote speaker-diarization-community-1** — https://huggingface.co/pyannote/speaker-diarization-community-1

Runtimes / libraries: `parakeet-mlx` (Apache-2.0), `pyannote.audio` (MIT), MLX, PyTorch.

The control panel's UI typeface is **Figtree** by Erik Kennedy
(https://github.com/erikdkennedy/figtree), licensed under the **SIL Open Font
License 1.1**. The variable-font woff2 files are vendored in
`gui/static/fonts/` with the license text (`OFL.txt`) alongside, and the
panel serves them itself (`GET /static/fonts/...`), never from a CDN.

If transcripts or derived outputs are redistributed outside DPSCD, keep this
attribution with them.
