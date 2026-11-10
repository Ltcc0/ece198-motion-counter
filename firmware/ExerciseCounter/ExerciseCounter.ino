/*
  ExerciseCounter — Arduino UNO R4 Minima, FW=3 PASSAGE
  HC-SR04: VCC -> 5V, GND -> GND, Trig -> D9, Echo -> D10.
  Optional B1K potentiometer: outer legs -> 5V/GND, middle leg -> A0.
  Connect it securely before changing USE_POT to true.
  Serial Monitor: 115200 baud, Newline (or Both NL & CR).
  Commands: HELP, START, STOP, STATUS, LOG.

  STOP saves a summary. An unfinished session is not saved on power loss.
  Checksums reject invalid records; they do not guarantee power-fail atomicity.
*/
#include <EEPROM.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <ctype.h>

const uint8_t TRIG_PIN = 9, ECHO_PIN = 10, POT_PIN = A0;
const bool USE_POT = false; // First build: sensor only; do not connect the knob.
const float FIXED_NEAR_CM = 25.0f; // Entry threshold; clear threshold is +10 cm.
const unsigned long SAMPLE_MS = 70, DISPLAY_MS = 500;
const unsigned long CLEAR_CONFIRM_MS = 300;
const uint8_t STABLE_SAMPLES = 3, LOG_SLOTS = 16;
const uint32_t RECORD_MAGIC = 0x45435831UL;
const uint8_t COMMITTED = 0xA5;

struct Record {
  uint32_t magic, id, count, seconds, checksum;
  uint8_t committed, reserved[3];
};
static_assert(sizeof(Record) == 24, "Unexpected record size");

enum MotionPhase { WAIT_CLEAR, ARMED, OCCUPIED };
MotionPhase phase = WAIT_CLEAR;
bool running = false, distanceValid = false, serialWasConnected = false;
bool clearActive = false;
const char *sensorStatus = "NO_ECHO";
uint8_t nearStreak = 0;
float distanceCm = 0;
float nearCm = FIXED_NEAR_CM, farCm = FIXED_NEAR_CM + 10.0f;
uint32_t count = 0, lastSavedId = 0, sessionId = 0, finalSeconds = 0;
unsigned long startedAt = 0, lastSampleAt = 0, lastDisplayAt = 0;
unsigned long clearStartedAt = 0;
char commandBuffer[48];
uint8_t commandLength = 0;
bool commandOverflow = false;

uint32_t recordChecksum(const Record &r) {
  // Hash only the four defined data fields, not padding or commit marker.
  const uint32_t words[] = {r.magic, r.id, r.count, r.seconds};
  uint32_t hash = 2166136261UL;
  for (uint8_t i = 0; i < 4; ++i)
    for (uint8_t b = 0; b < 4; ++b) {
      hash ^= (words[i] >> (8 * b)) & 0xFF;
      hash *= 16777619UL;
    }
  return hash;
}

bool validRecord(const Record &r) {
  return r.magic == RECORD_MAGIC && r.id != 0 &&
         r.committed == COMMITTED && r.checksum == recordChecksum(r);
}

int recordAddress(uint8_t slot) { return slot * sizeof(Record); }

void loadHistory() {
  if (EEPROM.length() < LOG_SLOTS * sizeof(Record)) return;
  for (uint8_t slot = 0; slot < LOG_SLOTS; ++slot) {
    Record r;
    EEPROM.get(recordAddress(slot), r);
    if (validRecord(r) && r.id > lastSavedId) lastSavedId = r.id;
  }
}

bool saveSummary(uint32_t seconds) {
  if (EEPROM.length() < LOG_SLOTS * sizeof(Record)) return false;
  Record r = {};
  r.magic = RECORD_MAGIC;
  r.id = sessionId;
  r.count = count;
  r.seconds = seconds;
  r.checksum = recordChecksum(r);
  const int address = recordAddress((r.id - 1) % LOG_SLOTS);
  // Invalidate the old slot, write data, then mark it complete.
  EEPROM.update(address + offsetof(Record, committed), 0);
  EEPROM.put(address, r);
  EEPROM.update(address + offsetof(Record, committed), COMMITTED);
  Record check;
  EEPROM.get(address, check);
  if (!validRecord(check) || check.id != r.id || check.count != r.count ||
      check.seconds != r.seconds) return false;
  lastSavedId = sessionId;
  return true;
}

void printLog() {
  Serial.println("LOG_BEGIN id,count,seconds");
  uint32_t first = lastSavedId > LOG_SLOTS ? lastSavedId - LOG_SLOTS + 1 : 1;
  if (lastSavedId != 0) {
    for (uint32_t id = first; ; ++id) {
      Record r;
      EEPROM.get(recordAddress((id - 1) % LOG_SLOTS), r);
      if (validRecord(r) && r.id == id) {
        Serial.print(r.id); Serial.print(',');
        Serial.print(r.count); Serial.print(','); Serial.println(r.seconds);
      }
      if (id == lastSavedId) break;
    }
  }
  Serial.println("LOG_END (up to 16 saved sessions)");
}

void resetMotion() {
  phase = WAIT_CLEAR;
  nearStreak = 0;
  clearActive = false;
  clearStartedAt = 0;
}

void readThresholds() {
  // UNO R4 ADC is explicitly set to 10-bit in setup().
  nearCm = USE_POT ? 8 + (analogRead(POT_PIN) * 17L + 511) / 1023 : FIXED_NEAR_CM;
  farCm = nearCm + 10.0f;
}

uint32_t elapsedSeconds() {
  return running ? (millis() - startedAt) / 1000UL : finalSeconds;
}

const char *phaseName() {
  if (phase == ARMED) return "ARMED";
  if (phase == OCCUPIED) return "OCCUPIED";
  return "WAIT_CLEAR";
}

void printStatus() {
  Serial.print("STATE="); Serial.print(running ? "RUNNING" : "IDLE");
  Serial.print(" DIST_CM=");
  if (distanceValid) Serial.print(distanceCm, 1); else Serial.print("NO_TARGET");
  Serial.print(" SENSOR="); Serial.print(sensorStatus);
  Serial.print(" COUNT="); Serial.print(count);
  Serial.print(" SEC="); Serial.print(elapsedSeconds());
  Serial.print(" NEAR_CM="); Serial.print(nearCm, 1);
  Serial.print(" FAR_CM="); Serial.print(farCm, 1);
  Serial.print(" PHASE="); Serial.println(running ? phaseName() : "-");
}

void printHelp() {
  Serial.println("READY FW=3 MODE=PASSAGE: UNO R4 Minima + HC-SR04");
  Serial.println("HELP | START | STOP | STATUS | LOG");
  Serial.println("Set 115200 baud and Newline in Serial Monitor.");
  if (USE_POT) Serial.println("IDLE: turn knob; NEAR_CM=8..25, FAR_CM=NEAR_CM+10.");
  else Serial.println("Knob disabled. Thresholds use FIXED_NEAR_CM and +10 cm.");
  Serial.println("START locks thresholds and waits for a clear beam.");
  Serial.println("Keep sensor still. Move object up/down through its front beam.");
  Serial.println("One stable entry within 2..NEAR_CM counts once.");
  Serial.println("Rearm: clear >=FAR_CM or no echo continuously for 300 ms.");
  Serial.println("Too-close and middle-zone readings do not count or clear.");
  Serial.println("STOP saves summary; LOG reads the latest 16 saved summaries.");
}

void sampleDistance() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  unsigned long echoUs = pulseIn(ECHO_PIN, HIGH, 25000UL);
  distanceCm = echoUs / 58.0f;
  distanceValid = echoUs != 0 && distanceCm >= 2 && distanceCm <= 80;
  sensorStatus = echoUs == 0 ? "NO_ECHO" : distanceCm < 2 ? "TOO_CLOSE" :
                 distanceCm > 80 ? "TOO_FAR" : "OK";
  if (!running) return;
  unsigned long now = millis();
  bool entry = distanceValid && distanceCm <= nearCm;
  bool clear = echoUs == 0 || distanceCm > 80 ||
               (distanceValid && distanceCm >= farCm);

  // Only fresh valid near readings may count. A missing echo can establish
  // an empty beam, but never counts by itself. Short gaps cannot rearm.
  if (entry) {
    if (nearStreak < STABLE_SAMPLES) ++nearStreak;
    if (phase == ARMED && nearStreak >= STABLE_SAMPLES) {
      if (count < UINT32_MAX) ++count;
      phase = OCCUPIED;
      nearStreak = 0;
      if (Serial) {
        Serial.print("REP="); Serial.print(count);
        Serial.println(" EVENT=PASS_COUNTED next=CLEAR");
      }
    }
  } else nearStreak = 0;

  if (!clear) {
    clearActive = false; // Neutral/too-close/near breaks clear confirmation.
  } else {
    if (!clearActive) { clearActive = true; clearStartedAt = now; }
    if (phase != ARMED && now - clearStartedAt >= CLEAR_CONFIRM_MS) {
      bool initialClear = phase == WAIT_CLEAR;
      phase = ARMED;
      nearStreak = 0;
      if (Serial) Serial.println(initialClear ? "EVENT=CLEAR_READY next=ARMED" :
                                               "EVENT=CLEARED next=ARMED");
    }
  }
}

void handleCommand(char *text) {
  while (*text == ' ' || *text == '\t') ++text;
  size_t length = strlen(text);
  while (length && (text[length - 1] == ' ' || text[length - 1] == '\t'))
    text[--length] = '\0';
  if (!length) return;
  for (size_t i = 0; i < length; ++i)
    text[i] = toupper(static_cast<unsigned char>(text[i]));

  if (!strcmp(text, "HELP")) printHelp();
  else if (!strcmp(text, "STATUS")) printStatus();
  else if (!strcmp(text, "LOG")) {
    if (running) Serial.println("STOP before reading LOG.");
    else printLog();
  } else if (!strcmp(text, "START")) {
    if (running) { Serial.println("Already running; use STOP first."); return; }
    if (lastSavedId == UINT32_MAX) { Serial.println("Session ID limit reached."); return; }
    readThresholds();
    resetMotion();
    count = finalSeconds = 0;
    sessionId = lastSavedId + 1;
    startedAt = millis();
    running = true;
    Serial.print("STARTED id="); Serial.println(sessionId);
    Serial.println("Clear the beam first. Thresholds are locked.");
    printStatus();
  } else if (!strcmp(text, "STOP")) {
    if (!running) { Serial.println("Already stopped; no extra record saved."); return; }
    finalSeconds = elapsedSeconds();
    running = false;
    resetMotion();
    bool saved = saveSummary(finalSeconds);
    Serial.print(saved ? "SAVED id=" : "SAVE_FAILED id=");
    Serial.print(sessionId); Serial.print(" count="); Serial.print(count);
    Serial.print(" seconds="); Serial.println(finalSeconds);
  } else Serial.println("Unknown command. Use HELP, START, STOP, STATUS or LOG.");
}

void readCommands() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r' || c == '\n') {
      if (commandOverflow) Serial.println("Command too long; discarded. Use HELP.");
      else {
        commandBuffer[commandLength] = '\0';
        handleCommand(commandBuffer);
      }
      commandLength = 0;
      commandOverflow = false;
    } else if (!commandOverflow) {
      if (commandLength < sizeof(commandBuffer) - 1) commandBuffer[commandLength++] = c;
      else commandOverflow = true;
    }
  }
}

void setup() {
  pinMode(TRIG_PIN, OUTPUT);
  digitalWrite(TRIG_PIN, LOW);
  pinMode(ECHO_PIN, INPUT);
  analogReadResolution(10);
  Serial.begin(115200);
  loadHistory();
  readThresholds();
}

void loop() {
  bool connected = static_cast<bool>(Serial);
  if (connected && !serialWasConnected) printHelp();
  if (!connected && serialWasConnected) {
    commandLength = 0;
    commandOverflow = false;
  }
  serialWasConnected = connected;
  if (connected) readCommands();
  unsigned long now = millis();
  if (now - lastSampleAt >= SAMPLE_MS) {
    lastSampleAt = now;
    if (!running) readThresholds();
    sampleDistance();
  }
  if (connected && now - lastDisplayAt >= DISPLAY_MS) {
    lastDisplayAt = now;
    printStatus();
  }
}
