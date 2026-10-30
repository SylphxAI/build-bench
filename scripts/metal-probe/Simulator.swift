import UIKit
import Metal

final class ProbeDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        let device = MTLCreateSystemDefaultDevice()
        var result: [String: Any] = [
            "device": device?.name as Any? ?? NSNull(),
            "os": UIDevice.current.systemVersion,
            "model": UIDevice.current.model,
            "rendered": false
        ]
        #if arch(x86_64)
        result["architecture"] = "x86_64"
        #else
        result["architecture"] = "arm64"
        #endif

        if let device = device, let queue = device.makeCommandQueue(), let command = queue.makeCommandBuffer() {
            let descriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm, width: 4, height: 4, mipmapped: false)
            descriptor.storageMode = .shared
            descriptor.usage = [.renderTarget]
            if let texture = device.makeTexture(descriptor: descriptor) {
                let pass = MTLRenderPassDescriptor()
                pass.colorAttachments[0].texture = texture
                pass.colorAttachments[0].loadAction = .clear
                pass.colorAttachments[0].storeAction = .store
                pass.colorAttachments[0].clearColor = MTLClearColor(red: 0.25, green: 0.5, blue: 0.75, alpha: 1)
                if let encoder = command.makeRenderCommandEncoder(descriptor: pass) {
                    encoder.endEncoding()
                    command.commit()
                    command.waitUntilCompleted()
                    var pixels = [UInt8](repeating: 0, count: 64)
                    pixels.withUnsafeMutableBytes { bytes in
                        texture.getBytes(bytes.baseAddress!, bytesPerRow: 16, from: MTLRegionMake2D(0, 0, 4, 4), mipmapLevel: 0)
                    }
                    result["pixel_rgba"] = Array(pixels.prefix(4))
                    result["command_status"] = command.status.rawValue
                    result["rendered"] = command.status == .completed && abs(Int(pixels[0]) - 64) <= 1 && abs(Int(pixels[1]) - 128) <= 1 && abs(Int(pixels[2]) - 191) <= 1 && pixels[3] == 255
                    if let error = command.error { result["error"] = error.localizedDescription }
                }
            }
        }
        if let data = try? JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]),
           let directory = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first {
            try? data.write(to: directory.appendingPathComponent("metal.json"), options: .atomic)
            print(String(decoding: data, as: UTF8.self))
        }
        let controller = UIViewController()
        controller.view.backgroundColor = .systemBlue
        let label = UILabel(frame: CGRect(x: 20, y: 80, width: 340, height: 240))
        label.text = "Metal probe\n\(result)"
        label.numberOfLines = 0
        controller.view.addSubview(label)
        window = UIWindow(frame: UIScreen.main.bounds)
        window?.rootViewController = controller
        window?.makeKeyAndVisible()
        return true
    }
}

UIApplicationMain(CommandLine.argc, CommandLine.unsafeArgv, nil, NSStringFromClass(ProbeDelegate.self))
