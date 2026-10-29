/*
  Standalone Uno relay endurance/reset diagnostic. Serial: 115200.
  D4 -> IN1 (active LOW), D5 held OFF. Disconnect NMR TTL cables.
  s = NEW test from cycle 1; 0 = stop/OFF; ? = status.
  Never resumes automatically after reset. No NMR/GUI protocol.
  Uses EEPROM bytes 0..415 for a wear-levelled diagnostic journal.
*/
#include <Arduino.h>
#include <EEPROM.h>
#include <avr/wdt.h>
#include <stddef.h>

#if !defined(__AVR_ATmega328P__)
#error "Select Arduino Uno (ATmega328P)."
#endif

const uint8_t RELAY_PIN = 4;
const uint8_t UNUSED_RELAY_PIN = 5;
const bool ACTIVE_LOW = true;
const unsigned long ON_MS = 2000;
const unsigned long OFF_MS = 2000;
const uint32_t MAX_CYCLES = 1000;  // 0 = unlimited (EEPROM has finite endurance).

uint8_t resetFlags __attribute__((section(".noinit")));
void captureResetFlags() __attribute__((naked, section(".init3"), used));
void captureResetFlags() {
  resetFlags = MCUSR;
  MCUSR = 0;
  wdt_disable();
}

enum Stage : uint8_t { BEFORE_ON = 1, BEFORE_OFF = 2, COMPLETED = 3, STOP_REQUESTED = 4 };
struct __attribute__((packed)) Checkpoint {
  uint16_t magic;
  uint32_t sequence;
  uint32_t cycle;
  uint8_t stage;
  uint16_t crc;
};
// Explicit declarations keep Arduino's generated prototypes after these types.
uint16_t checksum(const Checkpoint &record);
void saveCheckpoint(Stage stage);
const uint16_t MAGIC = 0xD4C7;
const uint8_t SLOT_COUNT = 32;
static_assert(sizeof(Checkpoint) == 13, "Unexpected journal record layout");
Checkpoint latest = {};
int8_t latestSlot = -1;
bool running = false;
bool relayOn = false;
uint32_t cycle = 0;
unsigned long phaseStarted = 0;

uint8_t level(bool on) { return (on != ACTIVE_LOW) ? HIGH : LOW; }

uint16_t checksum(const Checkpoint &record) {
  const uint8_t *bytes = reinterpret_cast<const uint8_t *>(&record);
  uint16_t crc = 0xFFFF;
  for (size_t i = offsetof(Checkpoint, sequence); i < offsetof(Checkpoint, crc); ++i) {
    crc ^= bytes[i];
    for (uint8_t bit = 0; bit < 8; ++bit)
      crc = (crc & 1) ? (crc >> 1) ^ 0xA001 : crc >> 1;
  }
  return crc;
}

void loadCheckpoint() {
  for (uint8_t slot = 0; slot < SLOT_COUNT; ++slot) {
    Checkpoint candidate;
    EEPROM.get(slot * sizeof(Checkpoint), candidate);
    if (candidate.magic != MAGIC || candidate.crc != checksum(candidate) ||
        candidate.stage < BEFORE_ON || candidate.stage > STOP_REQUESTED) continue;
    if (latestSlot < 0 || static_cast<int32_t>(candidate.sequence - latest.sequence) > 0) {
      latest = candidate;
      latestSlot = slot;
    }
  }
}

void saveCheckpoint(Stage stage) {
  Checkpoint next = {MAGIC, latest.sequence + 1, cycle, stage, 0};
  next.crc = checksum(next);
  const uint8_t slot = (latestSlot + 1) % SLOT_COUNT;
  const int address = slot * sizeof(Checkpoint);
  const uint8_t *bytes = reinterpret_cast<const uint8_t *>(&next);
  // Invalidate first, write payload, commit magic LAST. On interrupted writes
  // the preceding valid record remains available (not necessarily this stage).
  EEPROM.update(address, 0);
  EEPROM.update(address + 1, 0);
  for (size_t i = sizeof(next.magic); i < sizeof(next); ++i)
    EEPROM.update(address + i, bytes[i]);
  EEPROM.update(address + 1, bytes[1]);
  EEPROM.update(address, bytes[0]);
  latest = next;
  latestSlot = slot;
}

void prefix() {
  Serial.print(F("[uptime_ms=")); Serial.print(millis());
  Serial.print(F("] CYCLE=")); Serial.print(cycle); Serial.print(' ');
}

void printStage(uint8_t stage) {
  switch (stage) {
    case BEFORE_ON: Serial.print(F("BEFORE_ON / ON_HOLD")); break;
    case BEFORE_OFF: Serial.print(F("BEFORE_OFF / OFF_HOLD")); break;
    case COMPLETED: Serial.print(F("COMPLETED")); break;
    case STOP_REQUESTED: Serial.print(F("STOP_REQUESTED")); break;
  }
}

void printCheckpoint() {
  if (latestSlot < 0) {
    Serial.println(F("LAST_CHECKPOINT none"));
    return;
  }
  Serial.print(F("LAST_CHECKPOINT cycle=")); Serial.print(latest.cycle);
  Serial.print(F(" stage=")); printStage(latest.stage); Serial.println();
  Serial.println(F("Checkpoint marks a command window, NOT valve/contact feedback."));
}

void printResetCause() {
  Serial.print(F("RESET_CAUSE MCUSR=0x")); Serial.print(resetFlags, HEX);
  if (resetFlags == 0) Serial.print(F(" UNKNOWN_BOOTLOADER"));
  if (resetFlags & _BV(PORF)) Serial.print(F(" POWER_ON"));
  if (resetFlags & _BV(EXTRF)) Serial.print(F(" EXTERNAL_RESET_OR_DTR"));
  if (resetFlags & _BV(BORF)) Serial.print(F(" BROWN_OUT"));
  if (resetFlags & _BV(WDRF)) Serial.print(F(" WATCHDOG"));
  Serial.println();
}

void switchRelay(bool on) {
  saveCheckpoint(on ? BEFORE_ON : BEFORE_OFF);
  prefix(); Serial.print(on ? F("COMMAND ON (D4 ") : F("COMMAND OFF (D4 "));
  Serial.print(level(on) == HIGH ? F("HIGH") : F("LOW")); Serial.println(')');
  Serial.flush();  // Send evidence BEFORE the potentially disruptive transition.
  digitalWrite(RELAY_PIN, level(on));
  relayOn = on;
  phaseStarted = millis();
  prefix(); Serial.println(on ? F("D4_WRITE_DONE ON") : F("D4_WRITE_DONE OFF"));
}

void stopTest() {
  // Record before turning OFF so a reset caused by STOP is also diagnosable.
  if (running || relayOn) saveCheckpoint(STOP_REQUESTED);
  running = false;
  digitalWrite(RELAY_PIN, level(false));
  relayOn = false;
  prefix(); Serial.println(F("STOPPED; D4/D5 commanded OFF"));
}

void setup() {
  digitalWrite(RELAY_PIN, level(false));
  digitalWrite(UNUSED_RELAY_PIN, level(false));
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(UNUSED_RELAY_PIN, OUTPUT);
  digitalWrite(8, LOW);
  pinMode(8, OUTPUT);  // No intentional NMR triggers; disconnect TTL for this test.
  Serial.begin(115200);
  Serial.println(F("\nBOOT RELAY_CYCLE_TEST; outputs OFF; automatic restart DISABLED"));
  printResetCause();
  loadCheckpoint();
  printCheckpoint();
  Serial.print(F("ON_MS=")); Serial.print(ON_MS);
  Serial.print(F(" OFF_MS=")); Serial.print(OFF_MS);
  Serial.print(F(" MAX_CYCLES=")); Serial.println(MAX_CYCLES);
  Serial.println(F("s = NEW test; 0 = STOP/OFF; ? = status. Close Bronkhorst GUI."));
  // Deliberately no watchdog: do not introduce watchdog resets into this test.
}

void loop() {
  // Bounded processing keeps timing responsive even with continuous serial input.
  for (uint8_t count = 0; count < 16 && Serial.available(); ++count) {
    const char command = Serial.read();
    if (command == 's' || command == 'S') {
      if (running) {
        Serial.println(F("Already running; send 0 first to start a NEW test."));
      } else {
        cycle = 1;
        running = true;
        Serial.println(F("NEW_TEST"));
        switchRelay(true);
      }
    } else if (command == '0') {
      stopTest();
    } else if (command == '?') {
      prefix(); Serial.println(running ? F("RUNNING") : F("STOPPED"));
      printCheckpoint();
    } else if (command != '\r' && command != '\n' && command != ' ' && command != '\t') {
      Serial.println(F("Unknown command; use s, 0 or ?"));
    }
  }
  if (!running) return;
  if (millis() - phaseStarted < (relayOn ? ON_MS : OFF_MS)) return;
  if (relayOn) {
    switchRelay(false);
  } else {
    saveCheckpoint(COMPLETED);
    prefix(); Serial.println(F("CYCLE_COMPLETED"));
    if ((MAX_CYCLES && cycle >= MAX_CYCLES) || cycle == UINT32_MAX) {
      running = false;
      Serial.println(F("TEST_FINISHED; output OFF"));
    } else {
      ++cycle;
      switchRelay(true);
    }
  }
}
