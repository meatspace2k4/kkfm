// kkfm-weather-watch: mirror the macOS Weather app's data whenever the app writes it.
//
// The Weather app's containers are protected; this binary is the only thing that gets
// Full Disk Access. It does no parsing, so changing how the weather is read never needs
// a rebuild (a rebuild changes the code hash and would need the permission granted again).
//
// On start and on every FSEvents change under the Weather containers it copies them to
// MIRROR (default ~/Library/Caches/kkfm-weather-mirror), then runs HOOK (default
// ~/.local/bin/kkfm-weather-hook) with the mirror path, which reads the condition and
// pushes it to the hourly player.
//
//   swiftc -O WeatherWatch.swift -o ~/.local/bin/kkfm-weather-watch
import CoreServices
import Foundation

let home = FileManager.default.homeDirectoryForCurrentUser.path
let env = ProcessInfo.processInfo.environment
let mirror = env["KKFM_WEATHER_MIRROR"] ?? home + "/Library/Caches/kkfm-weather-mirror"
let hook = env["KKFM_WEATHER_HOOK"] ?? home + "/.local/bin/kkfm-weather-hook"
let sources = [
    home + "/Library/Group Containers/group.com.apple.weather",
    home + "/Library/Containers/com.apple.weather/Data",
    home + "/Library/Containers/com.apple.weather.widget/Data",
    home + "/Library/Containers/com.apple.weather.menu/Data",
]

func log(_ s: String) {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    print("\(f.string(from: Date())) \(s)")
    fflush(stdout)
}

func run(_ args: [String]) -> Int32 {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: args[0])
    p.arguments = Array(args.dropFirst())
    do { try p.run() } catch { log("cannot run \(args[0]): \(error)"); return -1 }
    p.waitUntilExit()
    return p.terminationStatus
}

func syncAndHook() {
    for src in sources where FileManager.default.fileExists(atPath: src) {
        let dst = mirror + "/" + URL(fileURLWithPath: src).pathComponents.suffix(2).joined(separator: "_")
        try? FileManager.default.createDirectory(atPath: dst, withIntermediateDirectories: true)
        let rc = run(["/usr/bin/rsync", "-a", "--delete", "--exclude", "*.png", "--exclude", "*.heic",
                      src + "/", dst + "/"])
        if rc != 0 { log("rsync \(src) failed (\(rc)); Full Disk Access granted to this binary?") }
    }
    if FileManager.default.isExecutableFile(atPath: hook) { _ = run([hook, mirror]) }
}

var pending = false
let queue = DispatchQueue(label: "kkfm.weather")

let callback: FSEventStreamCallback = { _, _, _, _, _, _ in
    // Coalesce bursts: one sync half a second after the last write.
    queue.async {
        if pending { return }
        pending = true
        queue.asyncAfter(deadline: .now() + 0.5) { pending = false; syncAndHook() }
    }
}

log("watching \(sources.count) Weather containers -> \(mirror)")
queue.sync { syncAndHook() }
guard let stream = FSEventStreamCreate(nil, callback, nil, sources as CFArray,
                                       FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 0.2,
                                       FSEventStreamCreateFlags(kFSEventStreamCreateFlagFileEvents)) else {
    log("FSEventStreamCreate failed"); exit(1)
}
FSEventStreamSetDispatchQueue(stream, queue)
FSEventStreamStart(stream)
dispatchMain()
