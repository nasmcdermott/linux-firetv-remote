# Android official-app certificate pinning

## The hard constraint

The official Android app (`com.amazon.storm.lightning.client.aosp`, 4.9.1)
embeds Amazon's **“Turnstile Server”** certificate and builds its OkHttp
`TrustManager` from a KeyStore containing **only that certificate**. Hostname
verification is disabled, but trust is pinned to that single certificate.

There is **no Linux-side workaround**. TLS requires the private key for the
certificate the app trusts. The APK contains the public certificate only; the
private key is Amazon's. Changing the receiver's certificate, adding a system
CA, using a proxy, or using a different port does not help.

The official **iOS** app accepts the Linux self-signed certificate, so iOS is
the supported path today.

## The real workaround: bypass trust in the Android client

This is a client-side instrumentation change. It still runs the official app;
it does not reimplement the remote.

### Option A — Frida on a rooted Android device or emulator

1. Install `frida-tools` on the Linux host (a venv is fine):

   ```sh
   python3 -m venv .frida-venv
   .frida-venv/bin/pip install frida-tools
   .frida-venv/bin/frida --version
   ```

2. Download the matching `frida-server` for the Android device's ABI from
   <https://github.com/frida/frida/releases> (same version as the host
   `frida`), then:

   ```sh
   adb root
   adb push frida-server-<version>-android-<abi> /data/local/tmp/frida-server
   adb shell chmod 755 /data/local/tmp/frida-server
   adb shell /data/local/tmp/frida-server &
   adb forward tcp:27042 tcp:27042
   ```

3. Install/open the official Fire TV app, then spawn it with the bypass:

   ```sh
   .frida-venv/bin/frida -U -f com.amazon.storm.lightning.client.aosp \
     -l tools/android/frida-bypass.js --no-pause
   ```

   The script replaces the custom `TrustManager`, bypasses OkHttp's
   `CertificatePinner` if present, and keeps hostname verification permissive.
   The same bypass covers HTTPS 8080 and WSS 9090.

### Option B — LSPosed / Xposed module

On a rooted device (or Waydroid with LSPosed), enable a certificate-pinning
bypass module for the Fire TV package only, e.g. TrustMeAlready or
SSLUnpinning. Scope it to `com.amazon.storm.lightning.client.aosp`, then
force-stop and reopen the app. This is usually easier than Frida for daily
use, but it depends on the module working with the app's OkHttp version.

### Option C — Patched APK (no root)

Tools such as `objection patchapk` or `apk-mitm` can repackage the official APK
with a network-security config/TrustManager patch. This works on an unrooted
device, but:

- it modifies the official app;
- the patched APK must be kept in sync with app updates;
- do not redistribute the patched APK.

Use this only for personal testing.

### Option D — older app version

Some older official app builds may not contain this pinning. Check the exact
APK version before relying on it; older builds may also lack the current
Turnstile endpoints or voice support. This is not guaranteed.

## Waydroid caveat

Waydroid may work, but the Android app still needs:

1. A rooted Waydroid session or LSPosed/Frida support.
2. **Working SSDP multicast** from the Waydroid container to the LAN. Waydroid's
   default NAT often blocks multicast. If discovery does not show the receiver,
   use a bridged network or a real Android device. The receiver can still be
   reached by IP once the app knows it, but the app's discovery list is the
   usual entry point.

The receiver itself needs no changes for this. Its HTTPS/WSS certificates and
tokens are already correct; the Android client is the only blocker.

## Status

- iOS: works with the stock app.
- Android: requires one of the client-side bypasses above.
- No server-side fix exists without Amazon's private key.
