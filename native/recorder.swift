// stt-recorder — bot-free capture of a virtual meeting on this Mac.
//
// Captures the device microphone (what the user says) and system audio
// (what everyone else says, via a Core Audio process tap) through ONE
// private aggregate device — a single clock, so the two sources stay
// sample-aligned over an hour-long call with no drift correction.
// Writes an interleaved stereo PCM CAF: ch0 (L) = mic, ch1 (R) = system.
// CAF is growable and self-describing, so the file is valid even if this
// process is killed mid-write; the Python side transcodes it to m4a.
//
//   stt-recorder <output.caf> [--max-seconds N]
//
// Stop with SIGINT/SIGTERM (clean finalize). Exit codes: 0 ok, 2 permission
// or tap failure, 3 device/aggregate failure, 4 file/disk failure.
//
// Requires macOS 14.4+ (process taps). TCC: "System Audio Recording Only"
// + Microphone; the embedded Info.plist and ad-hoc signature give the
// binary a stable identity so the grants persist.

import AudioToolbox
import CoreAudio
import Foundation

// ---------- tiny property helpers -------------------------------------------

func propAddress(_ selector: AudioObjectPropertySelector,
                 scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal)
    -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope,
                               mElement: kAudioObjectPropertyElementMain)
}

func getProp<T>(_ objectID: AudioObjectID, _ selector: AudioObjectPropertySelector,
                scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                value: inout T) -> OSStatus {
    var addr = propAddress(selector, scope: scope)
    var size = UInt32(MemoryLayout<T>.size)
    return AudioObjectGetPropertyData(objectID, &addr, 0, nil, &size, &value)
}

func deviceUID(_ deviceID: AudioObjectID) -> String? {
    var uid: CFString = "" as CFString
    let err = getProp(deviceID, kAudioDevicePropertyDeviceUID, value: &uid)
    return err == noErr ? (uid as String) : nil
}

func defaultInputDevice() -> AudioObjectID? {
    var dev = AudioObjectID(kAudioObjectUnknown)
    let err = getProp(AudioObjectID(kAudioObjectSystemObject),
                      kAudioHardwarePropertyDefaultInputDevice, value: &dev)
    return (err == noErr && dev != kAudioObjectUnknown) ? dev : nil
}

// Launched via open(1)/LaunchServices, stderr goes nowhere — so the app writes
// its own log when --log is given (appending, same file the menu bar tails).
var logURL: URL?

func log(_ msg: String) {
    let ts = ISO8601DateFormatter().string(from: Date())
    let line = "[\(ts)] \(msg)\n".data(using: .utf8)!
    FileHandle.standardError.write(line)
    if let u = logURL {
        if let h = try? FileHandle(forWritingTo: u) {
            h.seekToEndOfFile(); h.write(line); try? h.close()
        } else {
            try? line.write(to: u)
        }
    }
}

func fail(_ code: Int32, _ msg: String) -> Never {
    log("FATAL: \(msg)")
    exit(code)
}

// ---------- arguments --------------------------------------------------------

let args = CommandLine.arguments
guard args.count >= 2 else { fail(2, "usage: stt-recorder <output.caf> [--max-seconds N] [--log <file>]") }
let outputURL = URL(fileURLWithPath: args[1])
var maxSeconds: Double = 4 * 3600
if let i = args.firstIndex(of: "--max-seconds"), i + 1 < args.count,
   let n = Double(args[i + 1]), n > 0 { maxSeconds = n }
if let i = args.firstIndex(of: "--log"), i + 1 < args.count {
    logURL = URL(fileURLWithPath: args[i + 1])
}

// ---------- the recorder -----------------------------------------------------

final class Recorder {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private var file: ExtAudioFileRef?
    private var fileRate: Double = 0
    private let ioQueue = DispatchQueue(label: "stt.recorder.io")
    private let tapDesc: CATapDescription
    private var stopped = false
    private var paused = false
    private var warnedFrames = false
    private var framesWritten = 0

    init() {
        // global tap: everything the system plays, from every process
        tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        tapDesc.uuid = UUID()
        tapDesc.muteBehavior = .unmuted  // the user keeps hearing the meeting
    }

    func start() {
        guard let micDev = defaultInputDevice(), let micUID = deviceUID(micDev) else {
            fail(3, "no default input device")
        }
        let err = AudioHardwareCreateProcessTap(tapDesc, &tapID)
        guard err == noErr, tapID != kAudioObjectUnknown else {
            fail(2, "could not create the system-audio tap (err \(err)) — " +
                    "check System Settings > Privacy & Security > Screen & System Audio Recording")
        }
        buildAggregate(micUID: micUID)
        openFile()
        startIO()
        log("recording: mic=\(micUID) -> L, system tap -> R, \(Int(fileRate)) Hz, cap \(Int(maxSeconds))s")

        // headphones plugged/unplugged mid-call: rebuild around the new mic.
        // Brief gap, no data loss; the CAF keeps growing at the same rate.
        var addr = propAddress(kAudioHardwarePropertyDefaultInputDevice)
        AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject), &addr, ioQueue) { [weak self] _, _ in
            self?.rebuildForNewInput()
        }
    }

    private func buildAggregate(micUID: String) {
        let desc: [String: Any] = [
            kAudioAggregateDeviceNameKey as String: "STT Recorder",
            kAudioAggregateDeviceUIDKey as String: "com.stt-workflow.recorder." + UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey as String: true,  // hidden from Sound settings
            kAudioAggregateDeviceMainSubDeviceKey as String: micUID,  // mic is the clock master
            kAudioAggregateDeviceSubDeviceListKey as String: [[kAudioSubDeviceUIDKey as String: micUID]],
            kAudioAggregateDeviceTapListKey as String: [[
                kAudioSubTapUIDKey as String: tapDesc.uuid.uuidString,
                kAudioSubTapDriftCompensationKey as String: true,  // tap resampled onto the mic clock
            ]],
            kAudioAggregateDeviceTapAutoStartKey as String: true,
        ]
        let err = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &aggID)
        guard err == noErr, aggID != kAudioObjectUnknown else {
            fail(3, "could not create the aggregate capture device (err \(err))")
        }
    }

    private func openFile() {
        var rate: Double = 0
        if getProp(aggID, kAudioDevicePropertyNominalSampleRate, value: &rate) != noErr || rate <= 0 {
            rate = 48000
        }
        // keep the file rate from the FIRST open — a mid-recording device swap
        // must keep appending at the same rate (the converter resamples)
        if fileRate == 0 { fileRate = rate }
        guard file == nil else { return }

        var fileFmt = AudioStreamBasicDescription(
            mSampleRate: fileRate, mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked,
            mBytesPerPacket: 4, mFramesPerPacket: 1, mBytesPerFrame: 4,
            mChannelsPerFrame: 2, mBitsPerChannel: 16, mReserved: 0)
        var f: ExtAudioFileRef?
        let err = ExtAudioFileCreateWithURL(outputURL as CFURL, kAudioFileCAFType,
                                            &fileFmt, nil, AudioFileFlags.eraseFile.rawValue, &f)
        guard err == noErr, let f else { fail(4, "could not create \(outputURL.path) (err \(err))") }
        file = f
        setClientFormat(rate: fileRate)
    }

    private func setClientFormat(rate: Double) {
        guard let file else { return }
        var clientFmt = AudioStreamBasicDescription(
            mSampleRate: rate, mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kLinearPCMFormatFlagIsFloat | kLinearPCMFormatFlagIsPacked,
            mBytesPerPacket: 8, mFramesPerPacket: 1, mBytesPerFrame: 8,
            mChannelsPerFrame: 2, mBitsPerChannel: 32, mReserved: 0)
        let err = ExtAudioFileSetProperty(file, kExtAudioFileProperty_ClientDataFormat,
                                          UInt32(MemoryLayout.size(ofValue: clientFmt)),
                                          &clientFmt)
        if err != noErr { fail(4, "could not set writer format (err \(err))") }
    }

    private func startIO() {
        // callbacks arrive ON ioQueue (not the realtime thread), so buffer
        // copies and file writes here are safe
        let err = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, ioQueue) {
            [weak self] _, inInputData, _, _, _ in
            self?.handle(inInputData)
        }
        guard err == noErr, procID != nil else { fail(3, "could not install the IO proc (err \(err))") }
        let startErr = AudioDeviceStart(aggID, procID)
        guard startErr == noErr else { fail(3, "could not start capture (err \(startErr))") }
    }

    // one IO cycle: sub-device streams come first in the buffer list, taps
    // are always appended last — so buffer[0] is the mic, the last buffer is
    // the system tap (HAL virtual format: Float32, non-interleaved per stream)
    // Pause/resume (SIGUSR1/SIGUSR2). The IO cycle keeps running — we simply stop
    // WRITING frames — so the audio device is never torn down and restarted, and
    // the paused span is just absent from the file. The recording is therefore the
    // concatenation of the parts you actually kept, which is what you want when
    // you step out of a meeting. Flipped on ioQueue (serial, the same queue the
    // IOProc block runs on) so it can't race a write mid-buffer.
    func setPaused(_ p: Bool) {
        ioQueue.async {
            guard !self.stopped, self.paused != p else { return }
            self.paused = p
            log(p ? "paused" : "resumed")
        }
    }

    private func handle(_ abl: UnsafePointer<AudioBufferList>) {
        guard !stopped, !paused, let file else { return }
        let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: abl))
        guard buffers.count >= 1 else { return }
        let micBuf = buffers[0]
        let sysBuf = buffers[buffers.count - 1]
        let haveTap = buffers.count >= 2

        func monoFrames(_ b: AudioBuffer) -> ([Float], Int) {
            let ch = max(1, Int(b.mNumberChannels))
            let n = Int(b.mDataByteSize) / (4 * ch)
            guard n > 0, let raw = b.mData?.assumingMemoryBound(to: Float.self) else { return ([], 0) }
            var out = [Float](repeating: 0, count: n)
            if ch == 1 {
                out.withUnsafeMutableBufferPointer { $0.baseAddress!.update(from: raw, count: n) }
            } else {  // interleaved multi-channel -> mean
                for i in 0..<n {
                    var acc: Float = 0
                    for c in 0..<ch { acc += raw[i * ch + c] }
                    out[i] = acc / Float(ch)
                }
            }
            return (out, n)
        }

        let (mic, nMic) = monoFrames(micBuf)
        let (sys, nSys) = haveTap ? monoFrames(sysBuf) : ([], 0)
        // max, NOT min: a side that delivers nothing (a missing TCC grant hands
        // over EMPTY buffers — no error, no prompt) must not starve the other.
        // min() zeroed whole recordings: the tap grant died, the mic was fine,
        // and every cycle wrote nothing. The dead side is padded with silence
        // and named in the log, so a half-granted setup still captures half.
        let n = max(nMic, nSys)
        guard n > 0 else { return }
        if !warnedFrames && (nMic == 0 || (haveTap && nSys == 0)) {
            warnedFrames = true
            if nMic == 0 {
                log("WARNING: the microphone is delivering NO data — grant "
                    + "Microphone in System Settings > Privacy & Security")
            }
            if haveTap && nSys == 0 {
                log("WARNING: the system-audio tap is delivering NO data — enable "
                    + "'STT Recorder' under System Settings > Privacy & Security > "
                    + "Screen & System Audio Recording (System Audio Recording Only); "
                    + "macOS shows no prompt for this one")
            }
        }

        var inter = [Float](repeating: 0, count: n * 2)
        for i in 0..<n {
            inter[i * 2] = i < nMic ? mic[i] : 0        // L = mic
            inter[i * 2 + 1] = i < nSys ? sys[i] : 0    // R = system
        }
        var written = inter  // ExtAudioFileWrite needs a mutable base pointer
        written.withUnsafeMutableBufferPointer { p in
            var outABL = AudioBufferList(
                mNumberBuffers: 1,
                mBuffers: AudioBuffer(mNumberChannels: 2,
                                      mDataByteSize: UInt32(n * 8),
                                      mData: UnsafeMutableRawPointer(p.baseAddress!)))
            let err = ExtAudioFileWrite(file, UInt32(n), &outABL)
            if err != noErr {
                log("write failed (err \(err)) — disk full? finalizing what we have")
                self.shutdown(code: 4)
            } else {
                framesWritten += n
            }
        }
    }

    private func rebuildForNewInput() {
        guard !stopped else { return }
        guard let micDev = defaultInputDevice(), let micUID = deviceUID(micDev) else {
            log("input device changed but none available; keeping the old aggregate")
            return
        }
        log("default input changed -> \(micUID); rebuilding capture")
        if let procID { AudioDeviceStop(aggID, procID); AudioDeviceDestroyIOProcID(aggID, procID) }
        procID = nil
        if aggID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggID) }
        aggID = kAudioObjectUnknown
        buildAggregate(micUID: micUID)
        var rate: Double = 0
        if getProp(aggID, kAudioDevicePropertyNominalSampleRate, value: &rate) == noErr, rate > 0 {
            setClientFormat(rate: rate)  // converter resamples onto the file's original rate
        }
        startIO()
    }

    func shutdown(code: Int32) {
        ioQueue.async { [self] in
            guard !stopped else { return }
            stopped = true
            if let procID { AudioDeviceStop(aggID, procID); AudioDeviceDestroyIOProcID(aggID, procID) }
            if aggID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggID) }
            if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
            if let file { ExtAudioFileDispose(file) }
            file = nil
            let secs = fileRate > 0 ? Double(framesWritten) / fileRate : 0
            log(String(format: "finalized %@ — %d frames (%.1fs)",
                       outputURL.lastPathComponent, framesWritten, secs))
            if framesWritten == 0 {
                log("WARNING: captured 0 frames — grant Microphone + " +
                    "'System Audio Recording Only' in System Settings > Privacy & Security")
            }
            exit(code)
        }
    }
}

// ---------- lifecycle --------------------------------------------------------

let recorder = Recorder()

for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { recorder.shutdown(code: 0) }
    src.activate()
    _ = Unmanaged.passRetained(src)  // keep the source alive for the process lifetime
}

// SIGUSR1 pauses, SIGUSR2 resumes. Explicit rather than one toggling signal, so a
// duplicate or lost signal can never leave the recorder in the opposite state to
// what the menu bar believes.
for (sig, wantPaused) in [(SIGUSR1, true), (SIGUSR2, false)] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { recorder.setPaused(wantPaused) }
    src.activate()
    _ = Unmanaged.passRetained(src)
}

DispatchQueue.main.asyncAfter(deadline: .now() + maxSeconds) {
    log("max duration reached — stopping")
    recorder.shutdown(code: 0)
}

recorder.start()
dispatchMain()
