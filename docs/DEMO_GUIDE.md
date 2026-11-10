# Demonstration guide

This guide describes the FW3 **Passage mode** prototype. One detected passage counts as one. A downward passage followed by a separate upward passage counts as two, provided the detection area clears between them.

## Everyday launch after the first firmware upload

Connect the wired Arduino to the Mac by USB, run `./start-demo.command`, wait for the dashboard to connect, and click **Start session**. The button sends `START` to the board automatically. **Stop & save** sends `STOP` automatically. No Arduino IDE window or manually typed serial command is needed during the demonstration.

The firmware starts running when the board receives power. The dashboard starts and stops a counting session; its buttons do not upload firmware.

## Before presenting

1. Upload `firmware/ExerciseCounter/ExerciseCounter.ino` to the **Arduino UNO R4 Minima** if it is not already installed.
2. Close Arduino IDE's **Serial Monitor** and **Serial Plotter**.
3. Keep the sensor fixed. Position the object so that each passage crosses the sensor's front detection area at about 10–20 cm.
4. Run `./start-demo.command` from the project folder and keep its Terminal window open.
5. Open `http://127.0.0.1:8765` and check that device readings update. If the Arduino has not connected automatically, use **Device setup** to select its USB port and click **Connect device**.
6. Clear the detection area. Use slow, deliberate passages for the first check.

The dashboard and USB bridge run together on the Mac. The embedded program already runs on the Arduino after upload; there is no separate embedded service to start on the computer.

The main page contains the counter, duration and live distance, with phase feedback beneath the session controls. **Session records** in the header opens a separate dialog containing **Refresh** and **Export CSV**. **Device setup** opens the connection drawer.

## Short presentation script

> Our project explores simple movement guidance and participation records for home exercise after the STEPS program. A clinician would define the exercise and who it is suitable for. This is our classroom bench prototype.
>
> An ultrasonic sensor measures distance. The Arduino recognizes a stable object entering its detection area and counts one passage. Our computer dashboard shows the count, device state and saved session summaries.
>
> In this demonstration, one passage counts as one. Down and up are two separate passages when the area clears between them. We are demonstrating the sensing and recording process; the count does not establish exercise quality or clinical benefit.

## Live sequence: five passages

| Action | What to show or say |
| --- | --- |
| Click **Start session** with the object out of the detection area. | “The device first waits for a clear area. Ready for your next pass means it is armed.” |
| Slowly pass the object through the front detection area. | “This entry counts as one passage.” |
| Hold the object in front for a moment. | “Holding it still does not keep adding counts.” |
| Move it fully out and wait for **Ready for your next pass** again. | “The area must remain clear for at least 0.3 seconds before another passage.” |
| Repeat until five deliberate passages have been made. | Compare the displayed number with the manual count. If they differ, report the observed result rather than assuming the target was achieved. |
| Click **Stop & save** and confirm the saved summary. | “The Arduino stores the session number, count and elapsed time in its local memory.” |
| Click the header's **Session records** button. Use **Refresh** or **Export CSV** in that dialog if useful. | “These are compact participation records for later review.” |

After presenting, confirm the session was saved, then press **Control+C** in the server's Terminal window.

## How the parts work together

- **HC-SR04:** provides distance measurements. It does not identify a person or recognize a prescribed exercise.
- **Arduino firmware:** confirms three consecutive readings at 2–25 cm, counts one entry, waits for a clear area, times the session and saves its summary on STOP.
- **Local USB bridge:** reads the board's serial messages and passes dashboard commands to the board.
- **English dashboard:** displays live readings, state and history, and provides the computer controls replacing the proposed OLED/buttons.

The board samples approximately every 70 ms. Very fast passages may not provide three valid readings. Clear means a measurement of at least 35 cm, no echo, or a reading beyond the accepted 80 cm display range, sustained for at least 300 ms. Readings between 25 and 35 cm or below 2 cm do not establish a clear area.

The board saves the latest 16 completed summaries. It does not save the current unfinished session continuously. Imported board records do not contain a mode label; without separately recorded mode information, their mode is **Not recorded**. They may come from firmware that counted full approach-and-return cycles; keep that uncertainty when comparing results.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No USB port appears. | Use a USB cable that supports data, connect the board securely, and check the port list again. A powered board alone does not prove that USB data is working. On a Mac, select the actual `/dev/cu.usbmodem...` device, not Bluetooth or debug-console. |
| Port busy, connection fails, or upload fails while the dashboard is connected. | Close Serial Monitor and Serial Plotter, and close any other program reading the same port. Disconnect the dashboard before uploading firmware, then reconnect afterward. |
| The browser cannot open the dashboard. | Confirm the server's Terminal window is still running and use `http://127.0.0.1:8765`. Start only one copy of the server. |
| **Start session** is disabled. | Wait for a fresh device reading and passage-mode firmware identification. If disconnected, open **Device setup** and connect the Arduino port. Starting from the webpage requires no manually typed Arduino command. |
| The count stays at zero and the page says **Clear the path to begin** (`WAIT_CLEAR`). | Start with the detection area empty and allow at least 0.3 seconds of clear readings. Starting with an object already in front intentionally does not count it. |
| The page says **Ready for your next pass** (`ARMED`), but a passage does not count. | Cross directly in front at 2–25 cm and move more slowly. Use a flat, sufficiently large target facing the sensor. Three consecutive valid near readings are required. |
| The page stays at **Pass counted · move fully away** (`OCCUPIED`). | Fully leave the detection area or move beyond 35 cm. Wait at least 0.3 seconds and look for **Ready for your next pass** before reversing direction. Merely reaching 26–34 cm is insufficient. |
| The distance reads **Clear** while the area is empty (`NO_TARGET / NO_ECHO` in serial output). | This can be normal in Passage mode and can establish a clear area. It does not add a count by itself. Check the connection separately if the dashboard is disconnected or reports an error. |
| The distance stays **Clear** even with a target in front. | Disconnect power before checking VCC→5V, GND→GND, TRIG→9 and ECHO→10, shared breadboard rows and firm contacts. Reconnect and use a target facing the sensor within range. |
| A stationary object is counted again. | Check whether its echo disappears for at least 0.3 seconds. Such a loss is indistinguishable from an empty area in this mode and can rearm the counter. Stabilize the sensor, target and connections. |
| Too many fast up/down movements are missed. | Slow the movement and leave a distinct clear interval between crossings. No measured accuracy claim is established by the software alone. |
| A completed session is missing from board history. | Check for successful **Stop & save**. An unfinished session is lost on power loss, and only the latest 16 saved summaries remain on the board. |

## Scope to state clearly

The original pitch proposed home exercise guidance and concise participation records. This implementation uses a computer display and the explicitly selected passage-counting rule. It does not implement automatic calibration, exercise prescription, motion-quality assessment, emergency monitoring or automatic staff notification. The optional potentiometer is disabled in the default build. Use non-medical bench demonstration data.
