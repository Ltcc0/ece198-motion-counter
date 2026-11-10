# Serial protocol: FW3 Passage mode

Source of truth: [`firmware/ExerciseCounter/ExerciseCounter.ino`](../firmware/ExerciseCounter/ExerciseCounter.ino).

This is an integration reference. During normal use, **Start session** and **Stop & save** send the serial commands automatically through the local bridge. Users do not type them in Arduino IDE. The board runs the previously uploaded firmware when powered; these commands start or stop a session and do not flash firmware.

## Transport

- USB serial, **115200 baud**, normal 8-N-1 settings.
- Commands are ASCII text terminated by LF (`\n`), CR (`\r`) or CRLF. CRLF is handled as one command followed by an ignored empty line.
- Commands are case-insensitive. Leading and trailing spaces and tabs are ignored.
- The input buffer accepts at most **47 characters before the line ending**. Longer lines are discarded in full at the next line ending.
- Firmware uses `Serial.println` for output lines. Receivers should strip trailing CR/LF and tolerate lines unrelated to a pending command.
- Opening the serial connection produces the help banner. The firmware does not wait for a computer during startup.
- Only one program should own the serial port. Close Arduino IDE's Serial Monitor and Serial Plotter before connecting the bridge.

## Commands

| Command | Behavior | Principal response |
| --- | --- | --- |
| `HELP` | Print the version banner and usage text. | `READY FW=3 MODE=PASSAGE: UNO R4 Minima + HC-SR04` followed by help lines |
| `STATUS` | Print one current status line. | `STATE=...` |
| `START` | Start a new session, reset count/time, lock thresholds and enter `WAIT_CLEAR`. | `STARTED id=<id>` followed by a prompt and status |
| `STOP` | Stop a running session and attempt to save its summary to EEPROM. | `SAVED id=<id> count=<count> seconds=<seconds>` or `SAVE_FAILED ...` |
| `LOG` | While idle, print the latest valid saved summaries in ascending session-ID order. | `LOG_BEGIN ...`, zero or more CSV rows, `LOG_END ...` |

The command `CALIBRATE` is not implemented. The default fixed thresholds are 25 cm for entry and 35 cm for clear. `USE_POT` is false; enabling the optional knob requires a firmware change and secure wiring.

### State guards and input errors

The exact informational/error lines are:

```text
Already running; use STOP first.
Already stopped; no extra record saved.
STOP before reading LOG.
Session ID limit reached.
Command too long; discarded. Use HELP.
Unknown command. Use HELP, START, STOP, STATUS or LOG.
```

`START` does not restart an active session. Repeating `STOP` while idle does not create another record. `LOG` does not stop an active session. A successful serial write from the computer only means the command was sent; wait for the relevant board response before declaring completion or successful storage.

## Status lines

Status is emitted about every 500 ms while a serial client is connected, as well as in response to `STATUS` and immediately after a successful `START` command.

```text
STATE=RUNNING DIST_CM=18.4 SENSOR=OK COUNT=2 SEC=17 NEAR_CM=25.0 FAR_CM=35.0 PHASE=OCCUPIED
STATE=RUNNING DIST_CM=NO_TARGET SENSOR=NO_ECHO COUNT=2 SEC=18 NEAR_CM=25.0 FAR_CM=35.0 PHASE=ARMED
```

These are illustrative messages, not measured demonstration results.

| Field | Values and meaning |
| --- | --- |
| `STATE` | `IDLE` or `RUNNING` |
| `DIST_CM` | One decimal place for accepted readings between 2 and 80 cm inclusive; otherwise `NO_TARGET` |
| `SENSOR` | `OK`, `NO_ECHO`, `TOO_CLOSE` (below 2 cm), or `TOO_FAR` (above 80 cm) |
| `COUNT` | Unsigned 32-bit count of accepted passages in the current/latest RAM session |
| `SEC` | Elapsed whole seconds while running; the stopped session's final duration while idle |
| `NEAR_CM` | Entry threshold, normally 25.0 cm |
| `FAR_CM` | Clear threshold, normally 35.0 cm |
| `PHASE` | `WAIT_CLEAR`, `ARMED`, `OCCUPIED`; `-` while idle |

The status line does not include a session ID. `STARTED`, `SAVED` and historical log rows provide IDs. After a board restart, the RAM count/time start at zero even though saved EEPROM records remain available through `LOG`.

`NO_TARGET` is a sensor-reading state, not a USB connection state. A bridge must separately track whether the port is connected and whether messages remain fresh. No echo alone cannot distinguish an empty detection area from a disconnected or poorly aligned sensor.

## Passage states and events

1. `START` enters `WAIT_CLEAR`. An object already in front does not count.
2. Clear samples must continue for at least **300 ms**. Clear means no echo, a measured distance beyond 80 cm, or a valid reading at or above `FAR_CM`.
3. The board becomes `ARMED` and accepts a passage after **three consecutive valid readings** at or below `NEAR_CM`.
4. A counted passage changes the state to `OCCUPIED`. Holding the object near does not repeat the count.
5. Another continuous clear interval rearms the board.

The 2 cm minimum still applies to entry. Middle-zone readings (`NEAR_CM < distance < FAR_CM`) and `TOO_CLOSE` interrupt clear confirmation and do not count. Every non-entry sample resets the consecutive-entry confirmation. Sampling is approximately every 70 ms; this is not a guarantee that every rapid passage is detected.

Exact transition lines:

```text
EVENT=CLEAR_READY next=ARMED
REP=1 EVENT=PASS_COUNTED next=CLEAR
EVENT=CLEARED next=ARMED
```

`REP` is the updated **passage count** despite its historical name. The event hint `next=CLEAR` asks the user to clear the area; the actual firmware phase after counting is `OCCUPIED`. Events occur once per transition rather than on each repeated sample. No-echo samples can arm the device but can never increment the count by themselves.

## Saved records

Example response to `STOP`:

```text
SAVED id=4 count=5 seconds=22
```

Example response to `LOG`:

```text
LOG_BEGIN id,count,seconds
3,2,18
4,5,22
LOG_END (up to 16 saved sessions)
```

These examples illustrate the format only. Parse CSV rows only between the log delimiters. A log may contain zero records or gaps in IDs if a record is invalid. It is available only while idle.

EEPROM holds a ring of 16 records, each 24 bytes, containing magic, session ID, count, duration, checksum and a commit marker. The format is unchanged from earlier firmware. Invalid records are rejected, but the checksum/commit sequence does not guarantee atomic storage through arbitrary power failure.

Only `STOP` attempts to save the current session. Unfinished sessions are lost on power loss. There is no calendar timestamp, user identifier or counting-mode field in the record. Consequently, the mode of imported board records is **Not recorded** unless the bridge already has separate trustworthy metadata for that session. They must not automatically be labeled as Passage mode, and a computer's import time is not the original session time.

## Local HTTP bridge boundary

The local server at `http://127.0.0.1:8765` serves the buildless frontend and owns the serial connection. Its application endpoints are:

| HTTP endpoint | Request |
| --- | --- |
| `GET /api/health` | Identify the running local application |
| `GET /api/state` | Return the current bridge/dashboard snapshot |
| `GET /api/ports` | Return the currently enumerated serial ports |
| `GET /api/export.csv` | Download saved-session data as CSV |
| `POST /api/connect` | JSON `{ "port": "/dev/cu.usbmodem..." }` |
| `POST /api/disconnect` | JSON `{}` |
| `POST /api/command` | JSON `{ "command": "START" }`; allowed commands are `START`, `STOP`, `LOG`, `STATUS`, `HELP` |

POST requests must use `Content-Type: application/json` and an `Origin` matching the local HTTP origin, for example `http://127.0.0.1:8765`. They return `{ "ok": true/false, "message": "..." }`; acceptance is not a board acknowledgment. The frontend follows subsequent state/events to learn whether START, STOP or LOG completed.

The state snapshot groups information under `connection`, `device`, `history`, `samples`, `events`, `ports` and `pending_command`, with `storage_error` for local archive failures. CSV columns are `device_id,id,count,seconds,saved_at,source,mode`; `saved_at` is a computer-observed save time when known, not a board-supplied calendar timestamp. The serial messages above remain the board's source of truth. See `backend/server.py` for the complete HTTP response schema and error handling.

The bridge's local session archive is `runtime/sessions.json`; it is excluded from Git. It complements the board's finite history and must not be confused with proof that a pending STOP has completed successfully.
