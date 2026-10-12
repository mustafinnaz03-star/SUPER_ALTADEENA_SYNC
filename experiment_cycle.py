"""Timing model and a deliberately non-hardware valve placeholder."""

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


class ValvePlaceholder:
    """Records intended state changes; never opens a port or sends a command."""

    def __init__(self, events):
        self.events = events
        self.is_open = False

    def set_open(self, is_open, point):
        self.is_open = is_open
        self.events.put(("valve_placeholder", (point, is_open)))
