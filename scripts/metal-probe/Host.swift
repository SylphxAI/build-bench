import Foundation
import Metal

let device = MTLCreateSystemDefaultDevice()
let result: [String: Any] = [
    "default_device": device?.name as Any? ?? NSNull(),
    "devices": MTLCopyAllDevices().map { $0.name },
    "os": ProcessInfo.processInfo.operatingSystemVersionString
]
let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
print(String(decoding: data, as: UTF8.self))
