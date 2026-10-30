#!/usr/bin/env bash
set -euo pipefail
probe_source=$(cd "$(dirname "$0")" && pwd)
probe_output=${1:?output directory required}
mkdir -p "$probe_output"
date -u > "$probe_output/read-utc.txt"
uname -m > "$probe_output/host-architecture.txt"
sw_vers > "$probe_output/macos.txt"
xcodebuild -version > "$probe_output/xcode.txt"
xcrun simctl list --json > "$probe_output/simulators.json"
xcrun swift "$probe_source/Host.swift" | tee "$probe_output/host-metal.json"
arch -x86_64 /usr/bin/true > "$probe_output/host-rosetta.txt" 2>&1 || true

probe_sdk=$(xcrun --sdk iphonesimulator --show-sdk-path)
for probe_arch in arm64 x86_64; do
  probe_bundle="com.sylphx.metalprobe.${probe_arch//_/-}"
  probe_app="$probe_output/MetalProbe-$probe_arch.app"
  mkdir -p "$probe_app"
  if ! xcrun --sdk iphonesimulator swiftc -target "$probe_arch-apple-ios18.0-simulator" -sdk "$probe_sdk" "$probe_source/Simulator.swift" -o "$probe_app/MetalProbe" > "$probe_output/compile-$probe_arch.log" 2>&1; then
    cat "$probe_output/compile-$probe_arch.log"
    continue
  fi
  cat > "$probe_app/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>$probe_bundle</string>
<key>CFBundleExecutable</key><string>MetalProbe</string>
<key>CFBundleName</key><string>MetalProbe</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>MinimumOSVersion</key><string>18.0</string>
<key>UIDeviceFamily</key><array><integer>1</integer><integer>2</integer></array>
<key>UILaunchScreen</key><dict/>
</dict></plist>
PLIST
  codesign --force --sign - "$probe_app" > "$probe_output/codesign-$probe_arch.log" 2>&1
done

python3 - "$probe_output/simulators.json" > "$probe_output/device-plan.tsv" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
devices = [dict(d, runtime=r) for r, ds in data['devices'].items() for d in ds if d.get('isAvailable') and 'iOS' in r]
for family, match in [('iphone', lambda n: n == 'iPhone 16 Pro Max'), ('ipad', lambda n: 'iPad Pro' in n and '13-inch' in n)]:
    choices = [d for d in devices if match(d['name'])]
    if not choices:
        raise SystemExit(f'No requested {family} simulator found')
    device = sorted(choices, key=lambda d: d['runtime'], reverse=True)[0]
    print(f"{family}\t{device['udid']}\t{device['runtime']}\t{device['name']}")
PY

while IFS=$'\t' read -r probe_family probe_udid probe_runtime probe_name; do
  xcrun simctl boot "$probe_udid" || true
  xcrun simctl bootstatus "$probe_udid" -b
  for probe_arch in arm64 x86_64; do
    probe_bundle="com.sylphx.metalprobe.${probe_arch//_/-}"
    probe_app="$probe_output/MetalProbe-$probe_arch.app"
    probe_log="$probe_output/$probe_family-$probe_arch-launch.log"
    if [ ! -f "$probe_app/MetalProbe" ]; then continue; fi
    if xcrun simctl install "$probe_udid" "$probe_app" > "$probe_log" 2>&1 && xcrun simctl launch --arch="$probe_arch" "$probe_udid" "$probe_bundle" >> "$probe_log" 2>&1; then
      for probe_wait in $(seq 1 20); do
        probe_container=$(xcrun simctl get_app_container "$probe_udid" "$probe_bundle" data)
        if [ -f "$probe_container/Documents/metal.json" ]; then
          cp "$probe_container/Documents/metal.json" "$probe_output/$probe_family-$probe_arch-metal.json"
          cat "$probe_output/$probe_family-$probe_arch-metal.json"
          break
        fi
        sleep 1
      done
    fi
    cat "$probe_log"
  done
  xcrun simctl shutdown "$probe_udid"
done < "$probe_output/device-plan.tsv"
python3 - "$probe_output" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
results = {p.name: json.loads(p.read_text()) for p in sorted(root.glob('*-metal.json'))}
(root / 'summary.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results, indent=2))
PY
