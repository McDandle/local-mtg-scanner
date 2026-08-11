import Foundation
import Vision
import ImageIO

// Usage: ocr <image-path>  → JSON array of recognized text lines with
// normalized bounding boxes (Vision origin is bottom-left).

guard CommandLine.arguments.count > 1 else {
    FileHandle.standardError.write("usage: ocr <image>\n".data(using: .utf8)!)
    exit(1)
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
      let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
    FileHandle.standardError.write("could not read image\n".data(using: .utf8)!)
    exit(2)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
// English first, plus the languages most common on non-English Magic cards
// (LTR, BLB, etc.). Vision picks per-line, so English cards aren't hurt.
request.recognitionLanguages = ["en-US", "fr-FR", "de-DE", "es-ES", "it-IT", "pt-BR"]

let handler = VNImageRequestHandler(cgImage: img, options: [:])
do {
    try handler.perform([request])
} catch {
    FileHandle.standardError.write("ocr failed: \(error)\n".data(using: .utf8)!)
    exit(3)
}

var results: [[String: Any]] = []
for obs in request.results ?? [] {
    guard let cand = obs.topCandidates(1).first else { continue }
    let b = obs.boundingBox
    results.append([
        "text": cand.string,
        "confidence": Double(cand.confidence),
        "x": Double(b.origin.x),
        "y": Double(b.origin.y),
        "w": Double(b.width),
        "h": Double(b.height),
    ])
}

let data = try JSONSerialization.data(withJSONObject: results)
print(String(data: data, encoding: .utf8)!)
