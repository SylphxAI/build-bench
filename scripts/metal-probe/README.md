# Metal simulator probe

Run `bash scripts/metal-probe/run.sh OUTPUT_DIRECTORY` on an Apple-silicon Mac
with Xcode and installed iPhone 16 Pro Max / 13-inch iPad Pro simulators.
The manual workflow runs the same probe on `macos-15` and `macos-latest` and
skips every hosted job when the repository is private.

The probe records the host Metal device, toolchain and simulator inventory,
then launches a small UIKit application compiled for both arm64 and x86_64.
Each application asks for a Metal device, clears a texture to a known color
and verifies the pixels read back after the GPU command completes. The x86_64
result checks the Rosetta simulator path separately from native execution.
Logs distinguish compile, install, launch, missing-result and render failures.
The probe contains no product code or credentials and captures no screenshots.

GitHub's [runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
read 2026-10-30 12:04 UTC, lists these labels as standard M1 arm64 runners,
free on public repositories. Its current arm64 limitations mention unsupported
nested virtualization but make no Metal guarantee. A positive Metal result
therefore requires the runtime probe; a product still needs its own rendered
frame before the environment can be accepted for that product.
