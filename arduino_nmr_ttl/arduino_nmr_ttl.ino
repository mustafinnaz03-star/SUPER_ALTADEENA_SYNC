/*
  Bronkhorst GUI <-> Arduino <-> NMR TTL handshake

  Upload this sketch to a conventional Arduino board (Uno/Nano/Mega etc.).
  It implements the protocol used by mass_flow/bronkhorst_gui.py:

    PC -> Arduino:  TRIGGER <run-id> <point>
    Arduino -> PC:  TRIGGERED <run-id> <point>
    NMR -> Arduino: rising TTL edge on NMR_DONE_PIN
    Arduino -> PC:  NMR_DONE <run-id> <point>

  A new TRIGGER is rejected while the board is waiting for NMR_DONE.  A
  completion edge received before MIN_NMR_DELAY_MS is ignored, preventing the
  outgoing trigger pulse from being mistaken for the returning signal.

  Hardware safety:
  - Connect GND of Arduino and the NMR TTL interface only if the two systems
    are explicitly specified to share a ground. Use an optocoupler/digital
    isolator where required.
  - Check TTL voltage and polarity with an oscilloscope before connecting to
    the NMR console. Arduino inputs must never receive a voltage outside the
    board's permitted range.
  - This board coordinates timing only; it is not an emergency gas shutdown.

  Pins below are defaults. Change them to fit your board/wiring. On an Uno,
  pin 2 supports external interrupts and is therefore used by default for the
  returning NMR TTL.
*/

#include <Arduino.h>
#include <ctype.h>
#include <stdlib.h>
#include <string.h>

// -------- User wiring settings --------
const uint8_t NMR_TRIGGER_PIN = 8;   // Arduino output -> NMR start input
const uint8_t NMR_DONE_PIN = 2;      // NMR record-done TTL -> Arduino input
const uint32_t SERIAL_BAUD = 115200;
const uint16_t TRIGGER_PULSE_MS = 5;  // Active-HIGH start pulse on D8, then LOW
const uint16_t MIN_NMR_DELAY_MS = 50;
// --------------------------------------

enum HandshakeState : uint8_t {
  IDLE,
  WAITING_FOR_NMR,
};

HandshakeState state = IDLE;

char activeRunId[40] = "";
long activePoint = 0;
unsigned long triggerAtMicros = 0;

volatile bool nmrEdgeSeen = false;
volatile unsigned long nmrEdgeMicros = 0;

char inputLine[112];
uint8_t inputLength = 0;

void onNmrDoneRisingEdge() {
  // Keep the interrupt handler tiny: the protocol state and Serial I/O are
  // handled safely from loop().
  nmrEdgeMicros = micros();
  nmrEdgeSeen = true;
}

bool isSafeToken(const char *token) {
  if (token == NULL || *token == '\0') {
    return false;
  }
  for (const char *cursor = token; *cursor; ++cursor) {
    const char value = *cursor;
    if (!(isalnum(static_cast<unsigned char>(value)) || value == '-' || value == '_' || value == '.')) {
      return false;
    }
  }
  return true;
}

bool parsePositivePoint(const char *token, long &point) {
  if (token == NULL || *token == '\0') {
    return false;
  }
  char *end = NULL;
  point = strtol(token, &end, 10);
  return end != token && *end == '\0' && point > 0;
}

void printError(const char *runId, const char *point, const char *reason) {
  Serial.print(F("ERROR "));
  Serial.print(runId && *runId ? runId : "unknown");
  Serial.print(' ');
  Serial.print(point && *point ? point : "0");
  Serial.print(' ');
  Serial.println(reason);
}

void clearPendingNmrEdge() {
  noInterrupts();
  nmrEdgeSeen = false;
  interrupts();
}

void emitTriggerPulse() {
  digitalWrite(NMR_TRIGGER_PIN, HIGH);
  delay(TRIGGER_PULSE_MS);
  digitalWrite(NMR_TRIGGER_PIN, LOW);
}

void startTrigger(const char *runId, const char *pointText) {
  long point = 0;
  if (!isSafeToken(runId) || !parsePositivePoint(pointText, point)) {
    printError(runId, pointText, "BAD_TRIGGER");
    return;
  }
  if (state != IDLE) {
    printError(runId, pointText, "BUSY_WAITING_FOR_NMR_DONE");
    return;
  }

  strncpy(activeRunId, runId, sizeof(activeRunId) - 1);
  activeRunId[sizeof(activeRunId) - 1] = '\0';
  activePoint = point;
  clearPendingNmrEdge();

  // Record the time before raising the output so that a short loopback pulse
  // cannot count as the NMR completion signal.
  triggerAtMicros = micros();
  emitTriggerPulse();
  state = WAITING_FOR_NMR;

  // GUI sends the next gas point only after this acknowledgement AND NMR_DONE.
  Serial.print(F("TRIGGERED "));
  Serial.print(activeRunId);
  Serial.print(' ');
  Serial.println(activePoint);
}

void abortTrigger(const char *runId, const char *pointText) {
  if (state == WAITING_FOR_NMR && strcmp(runId, activeRunId) == 0 && atol(pointText) == activePoint) {
    state = IDLE;
    clearPendingNmrEdge();
    Serial.print(F("ABORTED "));
    Serial.print(runId);
    Serial.print(' ');
    Serial.println(pointText);
  } else {
    printError(runId, pointText, "NO_MATCHING_TRIGGER");
  }
}

void handleCommand(char *line) {
  char *command = strtok(line, " \t");
  if (command == NULL) {
    return;
  }

  if (strcmp(command, "PING") == 0) {
    Serial.println(F("PONG"));
    return;
  }

  char *runId = strtok(NULL, " \t");
  char *point = strtok(NULL, " \t");
  char *extra = strtok(NULL, " \t");
  if (runId == NULL || point == NULL || extra != NULL) {
    printError(runId, point, "BAD_COMMAND_FORMAT");
    return;
  }

  if (strcmp(command, "TRIGGER") == 0) {
    startTrigger(runId, point);
  } else if (strcmp(command, "ABORT") == 0) {
    abortTrigger(runId, point);
  } else {
    printError(runId, point, "UNKNOWN_COMMAND");
  }
}

void pollSerial() {
  while (Serial.available() > 0) {
    const char incoming = static_cast<char>(Serial.read());
    if (incoming == '\r') {
      continue;
    }
    if (incoming == '\n') {
      inputLine[inputLength] = '\0';
      if (inputLength > 0) {
        handleCommand(inputLine);
      }
      inputLength = 0;
      continue;
    }
    if (inputLength >= sizeof(inputLine) - 1) {
      inputLength = 0;
      printError("unknown", "0", "LINE_TOO_LONG");
    } else {
      inputLine[inputLength++] = incoming;
    }
  }
}

void pollNmrCompletion() {
  if (state != WAITING_FOR_NMR) {
    return;
  }

  bool edgePending = false;
  unsigned long edgeAt = 0;
  noInterrupts();
  edgePending = nmrEdgeSeen;
  edgeAt = nmrEdgeMicros;
  nmrEdgeSeen = false;
  interrupts();

  if (!edgePending) {
    return;
  }

  const unsigned long elapsedMs = (edgeAt - triggerAtMicros) / 1000UL;
  if (elapsedMs < MIN_NMR_DELAY_MS) {
    // Treat this as crosstalk/loopback from the outgoing pulse, not a spectrum.
    return;
  }

  Serial.print(F("NMR_DONE "));
  Serial.print(activeRunId);
  Serial.print(' ');
  Serial.println(activePoint);
  state = IDLE;
  activeRunId[0] = '\0';
  activePoint = 0;
}

void setup() {
  pinMode(NMR_TRIGGER_PIN, OUTPUT);
  digitalWrite(NMR_TRIGGER_PIN, LOW);  // Never emit a trigger at boot.
  pinMode(NMR_DONE_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(NMR_DONE_PIN), onNmrDoneRisingEdge, RISING);

  Serial.begin(SERIAL_BAUD);
  delay(50);
  Serial.println(F("READY"));
}

void loop() {
  pollSerial();
  pollNmrCompletion();
}
