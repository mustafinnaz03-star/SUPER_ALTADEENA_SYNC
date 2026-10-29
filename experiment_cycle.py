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
RELAY_SELECTION_LABELS = {
    "1": "Одно реле: №1 · D4 → IN1",
    "2": "Одно реле: №2 · D5 → IN2",
    "3": "Два реле: D4 + D5 → IN1 + IN2",
}


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


@dataclass(frozen=True)
class ArduinoRecoveryPolicy:
    attempts: int = 3
    interval_s: float = 1.0

    def __post_init__(self):
        if type(self.attempts) is not int or not 1 <= self.attempts <= 100:
            raise ValueError("Количество попыток Arduino должно быть целым числом от 1 до 100")
        if not math.isfinite(self.interval_s) or not 0.1 <= self.interval_s <= 60:
            raise ValueError("Интервал между попытками Arduino должен быть от 0,1 до 60 с")

    def budget_s(self, startup_timeout_s):
        if not math.isfinite(startup_timeout_s) or startup_timeout_s <= 0:
            raise ValueError("Тайм-аут готовности Arduino должен быть конечным положительным числом")
        # READY + two OFF acknowledgements (3 s each) + 1 s settle + I/O margin.
        # Include the cooldown before EVERY attempt, including the first one.
        return self.attempts * (startup_timeout_s + 9 + self.interval_s)


class ArduinoValveController:
    """Switch selected coils, publishing only acknowledged output states.

    False: both coils unpowered. True: only relay_mask outputs are active.
    This is Arduino output confirmation, not physical valve position feedback.
    """

    def __init__(self, arduino, events, relay_mask=1):
        if type(relay_mask) is not int or relay_mask not in (1, 2, 3):
            raise ValueError("Выберите реле №1, №2 или оба")
        self.arduino = arduino
        self.events = events
        self.relay_mask = relay_mask
        self.is_switched = None

    def set_switched(self, switched, point):
        # A lost acknowledgement leaves the output state unknown. Cleanup must
        # send OFF even when the previous confirmed state was already OFF.
        self.is_switched = None
        self.events.put(("valve_status", (point, None, self.relay_mask)))
        result = self.arduino.submit("valves", {"switched": switched, "point": point, "relay_mask": self.relay_mask}, timeout_s=5)
        if result.get("switched") is not switched or result.get("relay_mask") != self.relay_mask:
            raise RuntimeError("Arduino не подтвердила требуемое состояние реле")
        self.is_switched = switched
        self.events.put(("valve_status", (point, switched, self.relay_mask)))
