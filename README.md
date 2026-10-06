# Cardputer CLI Buddy

English | [日本語](README.ja.md)

![Demo: approving a permission prompt, answering AskUserQuestion, and sending a kana prompt from the Cardputer](docs/demo.gif)

Drive multiple Claude Code CLI sessions from an M5Stack Cardputer-Adv over Wi-Fi or BLE.

- Answer permission dialogs with allow / deny (allow is only offered when the full request fits on screen)
- Answer AskUserQuestion prompts
- Send prompts
- Read session logs
- Type in Japanese kana (Tab cycles between alphanumeric / hiragana / katakana)
- Dictate a prompt in Japanese or English with the built-in mic, transcribed on-device on the Mac
- Show the battery level (and charging state) in the header

## Architecture

```mermaid
flowchart LR
  subgraph host["Host (macOS)"]
    cc1["Claude Code session<br>+ mod/"]
    cc2["Claude Code session<br>+ mod/"]
    d["daemon/ (buddyd)"]
  end
  dev["Cardputer-Adv<br>device/"]
  cc1 -- "HTTP over Unix socket<br>~/.cardbuddy/buddyd.sock" --> d
  cc2 -- "HTTP over Unix socket" --> d
  d <-- "Wi-Fi (TCP) or BLE (NUS)<br>AES-128-CTR + HMAC-SHA256" --> dev
```

| Directory | Role |
| --- | --- |
| `mod/` | Claude Code plugin. Its function hooks register the session, relay AskUserQuestion, inject prompts, and forward the session log. A bundled `PermissionRequest` command hook waits for the device's answer alongside the terminal dialog. |
| `daemon/` | buddyd (Python, bleak). Talks to the mod over the Unix socket `~/.cardbuddy/buddyd.sock` and to the device over Wi-Fi (TCP) or BLE. Includes the `buddy` CLI (`pair` / `status` / `install-agent`). |
| `device/` | MicroPython app for the Cardputer-Adv, a modified version of the buddy from [moremas/build-with-claude](https://github.com/moremas/build-with-claude) (Apache-2.0). |
| `PROTOCOL.md` | Wire protocol between buddyd and the device, and the threat model (Japanese). |
| `docs/daemon-api.md` | HTTP API between the mod and buddyd (Japanese). |
| `testvectors/` | Test vectors for the crypto layer, shared by the daemon and device tests. |

## Requirements

- M5Stack Cardputer-Adv
- macOS (`buddy install-agent` targets launchd; buddyd itself runs anywhere bleak does)
- Python 3.10+ and [uv](https://docs.astral.sh/uv/)
- Claude Code
- For voice input: macOS 26+ and Swift 6.2+ (Xcode or the Command Line Tools)

## Setup

Run the commands below from the repository root. The serial port name depends on your machine (`/dev/cu.usbmodem*` on macOS).

### 1. Flash the firmware

Flash UIFlow2 **v2.4.2**. Do not use a newer release.

- v2.4.3 and later (ESP-IDF 5.5) have a regression that leaves the Cardputer-Adv microphone silent ([m5stack/uiflow-micropython#97](https://github.com/m5stack/uiflow-micropython/pull/97), [espressif/esp-idf#18621](https://github.com/espressif/esp-idf/issues/18621)).
- In M5Burner, pick "UIFlow2.0 Cardputer-Adv" and select version 2.4.2.
- The Cardputer-Adv uses native USB, so download mode can only be entered with the buttons: hold BtnG0 on the back, press and release BtnRST, then release BtnG0.

On v2.4.2, the USB port shows up as a TinyUSB CDC device during normal boot. If the serial port does not come back after a reset, unplug and replug the USB cable.

### 2. Install the app on the device

```sh
uv --directory daemon run python ../device/scripts/deploy.py --port /dev/cu.usbmodemXXXX
```

`device/scripts/deploy.py` does the following:

1. Compiles the large modules (`crypto`, `buddy_protocol`, `buddy_ble`, `buddy_ui_cp`, `kana`) to `.mpy` with mpy-cross. The device has only about 60 KB of free memory, which is not enough to compile them from `.py` at import time.
2. Writes the `.mpy` files and the files that stay as `.py` (`main.py`, `apps/*.py`, and so on) to `/flash/`.
3. Removes any `.py` file on the device with the same name as a `.mpy`, because MicroPython imports `.py` in preference to `.mpy`.
4. Sets the NVS key `uiflow.boot_option` to 2, so the device boots into `/flash/main.py` instead of the UIFlow launcher.
5. Reboots the device.

The mpy-cross version must match the device's MicroPython. UIFlow2 v2.4.2 ships MicroPython 1.25 (mpy v6.3), which is the default `--mpy-cross 1.25`. mpy-cross is fetched with `uvx`. Without `--port`, the script only compiles and prints the result.

### 3. Share the key (`buddy pair`)

```sh
uv --directory daemon run buddy pair --port /dev/cu.usbmodemXXXX
```

- Generates a 32-byte key and writes it over USB to `~/.cardbuddy/key` (mode 0600) on the host and `/flash/cardbuddy.key` on the device. The key never goes over BLE.
- Saves the device's advertised name (`Claude_` followed by the last 6 hex digits of its BT MAC) to `~/.cardbuddy/device`. buddyd connects only to a device with exactly this name.
- Reuses the existing key if there is one. Pass `--rotate` to generate a new key.
- To use Wi-Fi, add `--wifi`. It prompts for the SSID and password (the password is not echoed) and writes them to `/flash/cardbuddy_wifi.json` on the device in plaintext.
- Reset the device afterwards so it loads the key.

### 4. Start buddyd

To run it in the foreground:

```sh
uv --directory daemon run buddyd
```

To start it automatically at login, register a launchd LaunchAgent:

```sh
uv --directory daemon run buddy install-agent
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sohsatoh.cardbuddy.buddyd.plist
```

- Logs go to `~/.cardbuddy/buddyd.log`.
- Check the status with `uv --directory daemon run buddy status`.
- After rotating the key, restart buddyd (`launchctl kickstart -k gui/$(id -u)/com.sohsatoh.cardbuddy.buddyd`).
- On first run, macOS may ask for permission to use Bluetooth.

Wi-Fi:

- buddyd listens on TCP port 47823 on all interfaces and broadcasts a UDP beacon (`cardbuddy/1 <port>`) to port 47824 every 2 seconds. The device finds the Mac through the beacon and connects to it, so the Mac and the device must be on the same network segment.
- If the macOS application firewall is on, macOS asks on first run whether to accept incoming connections for buddyd (Python). Allow it, or the device cannot connect over Wi-Fi.
- Wi-Fi is preferred. While a Wi-Fi session is up, buddyd stops scanning for BLE and drops any BLE connection. When the Wi-Fi session ends, buddyd goes back to BLE automatically. `buddy status` shows which one is in use.
- Change the port with `buddyd --tcp-port <port>`, or run BLE only with `buddyd --no-wifi` (edit `ProgramArguments` in the LaunchAgent plist to pass these under launchd).

### 5. Load the mod into Claude Code

Per invocation:

```sh
claude --plugin-dir /path/to/cardputer-cli-buddy/mod
```

To load it every time, set `CLAUDE_CODE_PLUGIN_DIRS` under `env` in your settings:

```json
{
  "env": {
    "CLAUDE_CODE_PLUGIN_DIRS": "/path/to/cardputer-cli-buddy/mod"
  }
}
```

Each session registered with buddyd gets a number from 1 to 9, shown as `Buddy #n` in the terminal status line. It matches the number in the device's session list.

### 6. Launch the app on the device

The device boots into a launcher (`main.py`). Select `claude_buddy` with `;` / `.` (or `,` / `/`, or `W` / `S`) and press Enter. Quitting the app with `Q` reboots the device back into the launcher.

### 7. Build the speech-to-text helper (voice input)

buddyd transcribes audio recorded on the device with `daemon/stt/stt`, which uses macOS 26's SpeechTranscriber on-device, so audio never leaves the Mac.

```sh
make -C daemon/stt
```

- If `swiftc` is not on your PATH, pass it explicitly: `make -C daemon/stt SWIFTC=/path/to/swiftc`.
- macOS downloads the speech model for each language (Japanese `ja-JP`, English `en-US`) the first time it is used. This needs network access and takes from a few to tens of seconds (about 12 s for English). To avoid the wait, fetch the models up front:

  ```sh
  daemon/stt/stt --lang ja-JP --prepare
  daemon/stt/stt --lang en-US --prepare
  ```

- No permission prompt appears: SpeechTranscriber uses neither the Speech Recognition privacy permission nor the Dictation setting (verified on macOS 26.5).
- Once the model is installed, up to 60 s of audio is transcribed in about 1–2 s on Apple Silicon.

## Keys

Pressed on their own, the Cardputer-Adv arrow keys type `;` `,` `.` `/`. Below, "↑↓" means either the bare `;` `.` keys or Fn+↑↓. Esc is the `` ` `` key.

| Screen | Keys |
| --- | --- |
| Session list | `1`–`9` / ↑↓ select, Enter compose a prompt, `l` or Fn+→ open the log, `v` voice input, `Q` quit |
| Log | ↑↓ scroll (older pages load at the top), `r` reload, Enter compose a prompt, Esc back to the list |
| Permission | `Y` allow, `N` deny, ↑↓ scroll long requests |
| Question | `1`–`4` / ↑↓ select, Space toggle (multi-select), Enter confirm |
| Prompt input | See below |
| Voice input | Enter or Space start / stop recording, Tab switch Japanese / English (when not recording), Esc cancel |

Prompt input keys:

| Key | Action |
| --- | --- |
| Fn+← / Fn+→ | Move the cursor left / right |
| Fn+↑ / Fn+↓ | Move the cursor up / down a line |
| Del | Delete the character before the cursor (pending romaji is deleted first) |
| Enter | Send |
| Esc | Cancel and go back |
| Tab | Cycle alphanumeric / hiragana / katakana |

- In the input screen, the bare `;` `,` `.` `/` keys type those characters.
- Kana are typed as romaji. There is no kanji conversion. Pending romaji is shown underlined in cyan and is committed before Enter, an arrow key, or Tab takes effect.
- Tab arrives with the same key code as `+`, so `+` cannot be typed.
- Prompts are limited to 500 characters.
- Voice input records up to 60 s. The transcript is placed in the prompt input for you to review and edit; it is sent only when you press Enter.
- When a permission request does not fit on screen in full (`full` is false), the device shows a warning to check the terminal and accepts only `N`.
- For 400 ms after a permission request or question appears, the confirming keys (`Y` / `N` / Enter) are ignored, so a keypress meant for the session list cannot answer it by accident.
- If a permission request or question arrives while you are typing, the input screen stays and shows the number of pending requests. Press Esc to go back and answer.

## Security

See [PROTOCOL.md](PROTOCOL.md) (Japanese) for the details and the threat model. In short:

- An application-layer encryption sits on top of BLE and TCP: AES-128-CTR with HMAC-SHA256 (encrypt-then-MAC), with per-connection session keys derived by HKDF-SHA256. Frames recorded from an earlier connection fail MAC verification when replayed.
- The shared key is written over USB and never sent over BLE or Wi-Fi.
- The Hello exchange proves that both sides hold the key. A host on the LAN that connects to buddyd's TCP port, or a fake beacon that lures the device, cannot establish a session.
- The device sends allow only for permission requests it could display in full.
- Out of scope:
  - Denial of service (a third-party central connecting first, jamming, fake beacons, flooding the TCP port, and so on; buddyd keeps at most 4 pending TCP handshakes, each limited to 5 seconds)
  - Traffic analysis
  - Key protection (the key and the Wi-Fi password are stored in plaintext on the host and the device)
  - Other processes running as the same user (the Unix socket is mode 0600, and the same user is trusted)

## Tests

Run the Python tests for the daemon, device, and mod together. The device tests run on the host against a fake M5 API. Tests that use the real `stt` binary run only after `make -C daemon/stt`.

```sh
uv --directory daemon run pytest -q tests stt ../device/tests ../mod/tests
```

Run the mod's TypeScript tests with:

```sh
claude plugin test mod
```

## Limitations

- At most 9 sessions are shown. Further sessions get no number and stay off the device until a slot frees up.
- Plan approval (ExitPlanMode) is not handled on the device; approve plans in the terminal.
- Only questions with 2 to 4 options are sent to the device. Others must be answered in the terminal.
- When buddyd is not connected to the device, or the session has no number, permission dialogs appear only in the terminal.
- No kanji conversion.
- UIFlow2 must stay on v2.4.2 (see "Flash the firmware").

## License

[Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for copyright and attribution.

`device/` is a modified version of the buddy from [moremas/build-with-claude](https://github.com/moremas/build-with-claude) (Apache-2.0). See [device/NOTICE](device/NOTICE) and [device/LICENSE-THIRD-PARTY.md](device/LICENSE-THIRD-PARTY.md) for the upstream copyright notice and the list of modifications.
