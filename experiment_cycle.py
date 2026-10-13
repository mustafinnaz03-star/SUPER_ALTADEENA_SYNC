"""Timing model and acknowledged Arduino relay control."""

from dataclasses import dataclass
import math


AUTOMATIC_MODES = ("automatic-arduino", "automatic-arduino-valves")
OPERATION_LABELS = {
    "manual": "Ручной",
    "automatic-arduino": "Авто + ЯМР",
    "automatic-arduino-valves": "Авто + ЯМР + клапаны",
}
CYCLE_DEFAULTS = {"t1": 60.0, "t2": 0.0, "t3": 0.0, "t4": 0.0}


@dataclass(frozen=True)
class CycleTiming:
    t1: float = 60.0
    t2: float = 0.0
    t3: float = 0.0
    t4: float = 0.0

    def __post_init__(self):
        if any(not math.isfinite(value) or value < 0 for value in (self.t1, self.t2, self.t3, self.t4)):
            raise ValueError("Задержки t1–t4 должны быть конечными числами не меньше нуля")

    @property
    def delay_s(self):
        return self.t1 + self.t2 + self.t3 + self.t4


class ArduinoValveController:
    """Switch both coils together, publishing only acknowledged output states.

    False: both coils unpowered (valve 1 open, valve 2 closed).
    True: both coils powered (valve 1 closed, valve 2 open).
    This is Arduino output confirmation, not physical valve position feedback.
    """

    def __init__(self, arduino, events):
        self.arduino = arduino
        self.events = events
        self.is_switched = None

    def set_switched(self, switched, point):
        # A lost acknowledgement leaves the output state unknown. Cleanup must
        # send OFF even when the previous confirmed state was already OFF.
        self.is_switched = None
        self.events.put(("valve_status", (point, None)))
        result = self.arduino.submit("valves", {"switched": switched, "point": point}, timeout_s=5)
        if result.get("switched") is not switched:
            raise RuntimeError("Arduino не подтвердила требуемое состояние реле")
        self.is_switched = switched
        self.events.put(("valve_status", (point, switched)))
