// stt-speakrs: speaker diarization with speakrs (Core ML) for the STT pipeline.
//
// Usage: stt-speakrs <models_dir> <in.wav> <out_dir> <ahc_threshold>
//
// Reads one 16 kHz mono 16-bit WAV. Writes four files to <out_dir>:
//   result.json   segments + exclusive_segments as [start, end, "SPEAKER_NN"],
//                 plus array shapes and timings
//   emb.f32       chunk embeddings, shape emb_shape (chunks, local speakers, dim)
//   clusters.i32  hard cluster id per chunk-speaker pair (-2 = inactive)
//   clean.f32     overlap-free active frames per chunk-speaker pair
// SPEAKER_NN in the segments equals hard cluster id NN, so Python builds one
// centroid per label from the rows of emb.f32 whose cluster id matches.
// The audio never leaves the machine: the Python caller runs this binary
// under a sandbox profile that denies all network access.
use speakrs::{AhcConfig, ExecutionMode, OwnedDiarizationPipeline};
use std::{fs, io::Write, path::Path, process::exit, time::Instant};

fn load_wav(path: &str) -> Result<Vec<f32>, String> {
    let data = fs::read(path).map_err(|e| format!("read wav: {e}"))?;
    if data.len() < 44 || &data[0..4] != b"RIFF" || &data[8..12] != b"WAVE" {
        return Err("not a RIFF/WAVE file".into());
    }
    let mut pos = 12usize;
    let mut fmt_ok = false;
    while pos + 8 <= data.len() {
        let size = u32::from_le_bytes([data[pos + 4], data[pos + 5], data[pos + 6], data[pos + 7]]) as usize;
        let body = pos + 8;
        if &data[pos..pos + 4] == b"fmt " && body + 16 <= data.len() {
            let fmt = u16::from_le_bytes([data[body], data[body + 1]]);
            let ch = u16::from_le_bytes([data[body + 2], data[body + 3]]);
            let sr = u32::from_le_bytes([data[body + 4], data[body + 5], data[body + 6], data[body + 7]]);
            let bits = u16::from_le_bytes([data[body + 14], data[body + 15]]);
            if fmt != 1 || ch != 1 || sr != 16000 || bits != 16 {
                return Err(format!("need 16 kHz mono 16-bit PCM, got fmt={fmt} ch={ch} sr={sr} bits={bits}"));
            }
            fmt_ok = true;
        } else if &data[pos..pos + 4] == b"data" {
            if !fmt_ok {
                return Err("data chunk before fmt chunk".into());
            }
            let end = (body + size).min(data.len());
            return Ok(data[body..end]
                .chunks_exact(2)
                .map(|b| i16::from_le_bytes([b[0], b[1]]) as f32 / 32768.0)
                .collect());
        }
        pos = body + size + (size & 1);
    }
    Err("no data chunk".into())
}

fn write_bytes(path: &Path, bytes: impl Iterator<Item = [u8; 4]>) -> Result<(), String> {
    let f = fs::File::create(path).map_err(|e| format!("create {}: {e}", path.display()))?;
    let mut w = std::io::BufWriter::new(f);
    for b in bytes {
        w.write_all(&b).map_err(|e| format!("write {}: {e}", path.display()))?;
    }
    w.flush().map_err(|e| format!("flush {}: {e}", path.display()))
}

fn run(args: &[String]) -> Result<(), String> {
    if args.len() != 5 {
        return Err("usage: stt-speakrs <models_dir> <in.wav> <out_dir> <ahc_threshold>".into());
    }
    let (models, wav, out_dir) = (&args[1], &args[2], Path::new(&args[3]));
    let ahc: f32 = args[4].parse().map_err(|_| format!("bad ahc threshold {:?}", args[4]))?;
    fs::create_dir_all(out_dir).map_err(|e| format!("out dir: {e}"))?;

    let t0 = Instant::now();
    let audio = load_wav(wav)?;
    let wav_s = t0.elapsed().as_secs_f64();

    let t1 = Instant::now();
    let mut pipeline = OwnedDiarizationPipeline::from_dir(Path::new(models), ExecutionMode::CoreMl)
        .map_err(|e| format!("load models: {e}"))?;
    let load_s = t1.elapsed().as_secs_f64();

    let t2 = Instant::now();
    let mut cfg = pipeline.pipeline_config();
    cfg.clustering = cfg
        .clustering
        .with_ahc(AhcConfig::new(ahc).map_err(|e| format!("ahc config: {e}"))?);
    let r = pipeline
        .run_with_config(&audio, "stt", &cfg)
        .map_err(|e| format!("diarize: {e}"))?;
    let run_s = t2.elapsed().as_secs_f64();

    let shape = r.segmentations.shape().to_vec();
    let (chunks, frames, speakers) = (shape[0], shape[1], shape[2]);
    // Overlap-free active frames per chunk-speaker pair: the same rule as
    // speakrs ChunkEmbeddings::training_set, so centroids built from rows with
    // enough clean frames match the ones speakrs clusters on.
    let mut clean = vec![0f32; chunks * speakers];
    for c in 0..chunks {
        for f in 0..frames {
            let mut sum = 0f32;
            for s in 0..speakers {
                sum += r.segmentations[[c, f, s]];
            }
            if (sum - 1.0).abs() < 1e-6 {
                for s in 0..speakers {
                    clean[c * speakers + s] += r.segmentations[[c, f, s]];
                }
            }
        }
    }
    write_bytes(&out_dir.join("emb.f32"), r.embeddings.iter().map(|v| v.to_le_bytes()))?;
    write_bytes(&out_dir.join("clusters.i32"), r.hard_clusters.iter().map(|v| v.to_le_bytes()))?;
    write_bytes(&out_dir.join("clean.f32"), clean.into_iter().map(|v| v.to_le_bytes()))?;

    let segs = |v: &Vec<speakrs::Segment>| -> Vec<serde_json::Value> {
        v.iter().map(|s| serde_json::json!([s.start, s.end, s.speaker])).collect()
    };
    let out = serde_json::json!({
        "engine": "speakrs-0.7.0-coreml",
        "ahc_threshold": ahc,
        "samples": audio.len(),
        "audio_sec": audio.len() as f64 / 16000.0,
        "wav_read_sec": wav_s,
        "model_load_sec": load_s,
        "pipeline_run_sec": run_s,
        "seg_shape": shape,
        "emb_shape": r.embeddings.shape(),
        "hard_clusters_shape": r.hard_clusters.shape(),
        "segments": segs(&r.segments),
        "exclusive_segments": segs(&r.exclusive_segments),
    });
    let tmp = out_dir.join("result.json.tmp");
    fs::write(&tmp, serde_json::to_vec(&out).unwrap()).map_err(|e| format!("write json: {e}"))?;
    fs::rename(&tmp, out_dir.join("result.json")).map_err(|e| format!("rename json: {e}"))?;
    eprintln!("run_sec={run_s:.2} load_sec={load_s:.2} segments={}", r.exclusive_segments.len());
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if let Err(e) = run(&args) {
        eprintln!("stt-speakrs: {e}");
        exit(1);
    }
}
