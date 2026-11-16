# Security

## Trust model

This is a **LAN-only** remote-control service, not an internet service. It
speaks the protocol used by the official Amazon Fire TV app so that the official
app can discover and control a Linux machine.

- The shared `x-api-key` is public by design (it is embedded in the app).
- The secret is the per-phone `x-client-token`, created by PIN pairing and
  stored in `~/.config/firetv-receiver/devices.json` (mode 0600).
- The TLS certificate is self-signed and generated locally in
  `~/.config/firetv-receiver/poc-tls/`.
- No Amazon certificate, private key, firmware, or account credential is
  included in this repository.

Do not expose TCP 8080 or 9090, or UDP 1900, to the internet. Do not reuse the
receiver certificate or the `devices.json` file outside your own machine.

## Pairing

- PINs are short-lived (60 seconds) and shown on the local desktop card or in
  the local journal.
- Five wrong attempts trigger throttling.
- Remove a phone you no longer use with `firetv-remote forget "<name>"` or
  `--all`.
- `./uninstall.sh --purge` removes pairings and the TLS identity.

## Android certificate pinning

The stock Android app pins Amazon's embedded Turnstile certificate. The private
key is not shipped in the APK, so there is no Linux-side fix. The official iOS
app accepts the receiver's self-signed certificate.

`tools/android/` documents client-side options (Frida, LSPosed, or a patched
APK). Those modify the app on your device and are your responsibility; only use
them with an app you are entitled to run.

## Voice and typed text

Voice audio is processed locally by faster-whisper. Nothing is sent to a cloud
service. Dictation is typed into the focused window, so treat the machine as
you would treat a local keyboard: anyone on the paired phone can type.

The safe-command gate prevents a phrase embedded in dictation from launching an
application, but it is a usability guard, not an authorization boundary.

## Reporting

Open a private GitHub security advisory on the repository rather than a public
issue for anything that could expose a pairing token, TLS key, or remote code
execution path. Include the affected version and a minimal reproduction.
