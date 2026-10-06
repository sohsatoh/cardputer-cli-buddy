// 呼び出し側は stdout の 1 行 JSON {"text", "error"} だけを解釈するので、診断は stderr に出す
// 音声も認識結果も stderr には出さない（PROTOCOL.md「音声入力」）
import AVFoundation
import Foundation
import Speech

func emit(_ text: String?, _ error: String?) -> Never {
    let obj: [String: Any] = ["text": text ?? NSNull(), "error": error ?? NSNull()]
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])
    FileHandle.standardOutput.write(data + Data("\n".utf8))
    exit(error == nil ? 0 : 1)
}

func log(_ s: String) { FileHandle.standardError.write(Data(("stt: " + s + "\n").utf8)) }

var lang = "", prepare = false, path: String?
var args = CommandLine.arguments.dropFirst()
while let a = args.popFirst() {
    switch a {
    case "--lang": lang = args.popFirst() ?? ""
    case "--prepare": prepare = true
    default: path = a
    }
}
guard ["ja-JP", "en-US"].contains(lang) else { emit(nil, "unsupported lang: \(lang) (use ja-JP or en-US)") }
guard prepare || path != nil else { emit(nil, "usage: stt --lang ja-JP|en-US (--prepare | <audio>)") }

guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: lang)) else {
    emit(nil, "on-device speech model for \(lang) is not available on this Mac (macOS 26 or later required)")
}
let transcriber = SpeechTranscriber(locale: locale, preset: .transcription)
let installed = await SpeechTranscriber.installedLocales.contains { $0.identifier(.bcp47) == locale.identifier(.bcp47) }
do {
    // installed でも request は nil にならないので、要求は常に実行し、表示だけ installed で分ける
    if let req = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
        if !installed { log("downloading the on-device speech model for \(lang); this happens only once and may take a minute") }
        let t = Date()
        try await req.downloadAndInstall()
        if !installed { log(String(format: "speech model for %@ installed in %.1fs", lang, Date().timeIntervalSince(t))) }
    }
} catch {
    emit(nil, "speech model for \(lang) could not be downloaded (check the network and retry): \(error.localizedDescription)")
}
if prepare { emit("", nil) }

let file: AVAudioFile
do { file = try AVAudioFile(forReading: URL(fileURLWithPath: path!)) } catch {
    emit(nil, "cannot read audio: \(error.localizedDescription)")
}
// 0 フレームの音声を SpeechAnalyzer に渡すと results が終わらず止まる（macOS 26.5）
if file.length == 0 { emit("", nil) }
do {
    let analyzer = SpeechAnalyzer(modules: [transcriber])
    async let text = transcriber.results.reduce("") { $0 + String($1.text.characters) }
    guard let last = try await analyzer.analyzeSequence(from: file) else { emit("", nil) }
    try await analyzer.finalizeAndFinish(through: last)
    emit(try await text, nil)
} catch {
    emit(nil, "transcription failed: \(error.localizedDescription)")
}
