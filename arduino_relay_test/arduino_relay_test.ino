/*
  Standalone Arduino Uno (ATmega328P) relay test, Serial 115200 baud.
  IN1 -> D4, IN2 -> D5. Default: active LOW relay module.
  Send '4' or '5' to toggle that relay, '0' to turn both off,
  '?' to print status. CR/LF and spaces are ignored.

  This sketch replaces the NMR sketch while uploaded; it implements no TTL
  handshake. ON/OFF describes the commanded relay state, not contact feedback.
*/
#include <Arduino.h>
#include <avr/wdt.h>

#if !defined(__AVR_ATmega328P__)
#error "Select Arduino Uno with ATmega328P for this sketch."
#endif

const uint8_t RELAY_1_PIN = 4;
const uint8_t RELAY_2_PIN = 5;
const bool RELAY_ACTIVE_LOW = true;  // Change to false for active HIGH modules.
const unsigned long HEARTBEAT_MS = 5000;

uint8_t resetFlags __attribute__((section(".noinit")));

// Capture flags before C++ globals are initialized. Some Uno bootloaders
// clear MCUSR before the application starts: report UNKNOWN in that case.
void captureResetFlags() __attribute__((naked, section(".init3"), used));
void captureResetFlags() {
  resetFlags = MCUSR;
  MCUSR = 0;
  wdt_disable();
}

bool relay1On = false;
bool relay2On = false;
unsigned long lastHeartbeat = 0;

uint8_t outputLevel(bool on) {
  return (on != RELAY_ACTIVE_LOW) ? HIGH : LOW;
}

void timestamp() {
  Serial.print(F("[uptime_ms="));
  Serial.print(millis());
  Serial.print(F("] "));
}

void printRelay(uint8_t pin, bool on) {
  timestamp();
  Serial.print(F("RELAY D"));
  Serial.print(pin);
  Serial.print(F(" command="));
  Serial.print(on ? F("ON") : F("OFF"));
  Serial.print(F(" pin="));
  Serial.println(outputLevel(on) == HIGH ? F("HIGH") : F("LOW"));
}

void setRelay(uint8_t pin, bool on) {
  digitalWrite(pin, outputLevel(on));
  printRelay(pin, on);
}

void printStatus() {
  printRelay(RELAY_1_PIN, relay1On);
  printRelay(RELAY_2_PIN, relay2On);
}

void printResetCause() {
  Serial.print(F("RESET_CAUSE MCUSR=0x"));
  Serial.print(resetFlags, HEX);
  if (resetFlags == 0) {
    Serial.println(F(" UNKNOWN (bootloader may have cleared flags)"));
    return;
  }
  if (resetFlags & _BV(PORF)) Serial.print(F(" POWER_ON"));
  if (resetFlags & _BV(EXTRF)) Serial.print(F(" EXTERNAL_RESET (button or USB/DTR)"));
  if (resetFlags & _BV(BORF)) Serial.print(F(" BROWN_OUT"));
  if (resetFlags & _BV(WDRF)) Serial.print(F(" WATCHDOG"));
  Serial.println();
}

void setup() {
  // Set the output latch BEFORE switching the pin to OUTPUT to avoid a
  // short active-LOW pulse during setup.
  digitalWrite(RELAY_1_PIN, outputLevel(false));
  digitalWrite(RELAY_2_PIN, outputLevel(false));
  pinMode(RELAY_1_PIN, OUTPUT);
  pinMode(RELAY_2_PIN, OUTPUT);

  Serial.begin(115200);
  Serial.println(F("BOOT relay-test D4 D5; both relays commanded OFF"));
  printResetCause();
  Serial.print(F("POLARITY="));
  Serial.println(RELAY_ACTIVE_LOW ? F("ACTIVE_LOW") : F("ACTIVE_HIGH"));
  Serial.println(F("COMMANDS: 4=toggle D4, 5=toggle D5, 0=both OFF, ?=status"));
  printStatus();
  Serial.flush();
  wdt_enable(WDTO_2S);
}

void loop() {
  wdt_reset();
  // Limit work per loop so even a continuous input stream cannot prevent
  // heartbeat output and watchdog servicing.
  for (uint8_t count = 0; count < 16 && Serial.available() > 0; ++count) {
    const char command = static_cast<char>(Serial.read());
    switch (command) {
      case '4':
        relay1On = !relay1On;
        setRelay(RELAY_1_PIN, relay1On);
        break;
      case '5':
        relay2On = !relay2On;
        setRelay(RELAY_2_PIN, relay2On);
        break;
      case '0':
        relay1On = relay2On = false;
        setRelay(RELAY_1_PIN, false);
        setRelay(RELAY_2_PIN, false);
        break;
      case '?':
        printStatus();
        break;
      case '\r': case '\n': case ' ': case '\t':
        break;
      default:
        timestamp();
        Serial.print(F("ERROR unknown command byte=0x"));
        Serial.println(static_cast<uint8_t>(command), HEX);
        break;
    }
    wdt_reset();
  }
  const unsigned long now = millis();
  if (now - lastHeartbeat >= HEARTBEAT_MS) {
    lastHeartbeat = now;
    timestamp();
    Serial.print(F("ALIVE D4="));
    Serial.print(relay1On ? F("ON") : F("OFF"));
    Serial.print(F(" D5="));
    Serial.println(relay2On ? F("ON") : F("OFF"));
  }
}
