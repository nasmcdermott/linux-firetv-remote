# Troubleshooting

## The phone app does not list the machine

- Both devices must be on the same LAN/VLAN; guest Wi-Fi isolation and some
  Docker/Waydroid NATs block SSDP multicast.
- Check UDP 1900 and TCP 60000/8009/8080 are not firewalled.
- Confirm the receiver is up: `firetv-remote status` and
  `journalctl --user -u firetv-receiver -f`.
- If discovery is blocked but you know the address, add the machine by IP in
  the app where the app allows it.

## No PIN card

- `firetv-remote pin` prints the current PIN from the log.
- PySide6 or `layer-shell-qt` may be missing; see the package table in
  [INSTALL.md](INSTALL.md). Pairing still works without the card.
- On a TTY or headless session there is no card; use the CLI PIN.
- `firetv-remote preview-pin --ttl 5` tests the card without pairing.

## PIN rejected

- PINs expire after 60 seconds; request a new one.
- Five wrong attempts trigger throttling; wait and retry.
- If the phone was paired before and the token was lost, forget it:
  `firetv-remote forget "<name>"`, then pair again.

## Android cannot connect, iOS can

This is expected. The stock Android app pins Amazon's Turnstile certificate;
there is no Linux-side fix. See [`tools/android/README.md`](../tools/android/README.md).
The official iOS app accepts the receiver's self-signed certificate.

## Voice does not type or launch

- After a distro Python upgrade, the project `.venv` can be left pointing at
  the old interpreter and end up empty. `journalctl --user -u voice-stt -f`
  then shows `ModuleNotFoundError: No module named 'numpy'`. This is repaired
  automatically at the next service start or `firetv-remote` command; you can
  also run `python3 scripts/ensure_venv.py` by hand.
- The base voice path works on Python 3.15+; `ctranslate2` 4.9.0 provides the
  wheels. If a future Python is newer than the available wheels, the repair
  uses a `uv`-managed compatible interpreter.
- Wake-word detection is not part of the base install. Install it with
  `uv sync --extra wakeword` (Python 3.11-3.14 for now; `onnxruntime` does not
  have cp315 wheels yet). The Fire TV remote path does not need it.
- One voice service only: `voice-stt.service` and `voice-assistant.service`
  must not both run. Keep `voice-stt.service` in the merged setup.
- `journalctl --user -u voice-stt -f` should show the transcript. If it does
  but nothing types, `ydotool` cannot write `/dev/uinput`; install/enable
  `ydotoold` or use `wtype`/`xdotool`.
- If the transcript is wrong, try a larger model or adjust `hotwords` in
  `config/voice_assistant.yaml`.
- “Open …” inside a longer sentence is dictated by design. Use a short
  command such as “Open Firefox”.

## Voice and receiver fight over port 9090

`voice-stt.service` owns WSS 9090 in the merged install. The receiver detects
the busy port and reports `voice: external`. If you run `--phone-only`, the
receiver serves its own WSS instead; stop the other service first.

## Volume changes twice or not at all

- If `cec-remote-observer.service` owns volume, `remote-input` must not also
  send local media keys. The installer selects one path automatically.
- Without CEC, run `remote-input --portable` and ensure
  `config/keymap.json` has the local media-key overrides.
- Force the portable path for testing:
  `FIRETV_CEC_OWNED=0 ./install.sh --force-keymap`.

## Menu does nothing on non-KDE desktops

The generated `config/keymap.json` maps Menu to `KEY_MENU` and Home to `Super`.
Delete that file to return to built-ins, or regenerate it with
`./install.sh --force-keymap`. On KDE, `bash verify.sh` checks the D-Bus path.

If Menu stops working after a re-pair or distro upgrade, the remote's BLE
address probably changed. `remote-input.service` auto-detects the Amazon HID
device, so re-run `./install.sh` to refresh the installed unit; check
`journalctl --user -u remote-input -n 20` for `watching /dev/hidrawN`.

## BLE remote not found

- Pair/bond it first: `bluetoothctl devices`, then `scripts/01_pair_and_capture.sh`.
- Install `udev/91-fire-remote.rules` so the daemon can open the hidraw node;
  log out/in after adding your user to the `input` group.
- Set the MAC explicitly: `REMOTE_MAC=AA:BB:CC:DD:EE:FF ./install.sh`.
- `remote-input` and the capture scripts cannot share the device at the same
  time; stop the service before capturing.

## No systemd

Use `./install.sh --no-service`; it prints the foreground commands. Supervise
them with OpenRC, runit, s6, or your init system. `ydotoold` must run in the
background for dictation.

## Reinstalling or moving the checkout

Re-run `./install.sh` after moving the directory; the units contain the
checkout path. `./uninstall.sh` removes units and the launcher but keeps
pairings; `--purge` removes pairings, TLS identity and `.venv`.
