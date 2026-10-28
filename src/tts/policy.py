"""One timing policy for every video, preview, cache and repair pass."""
from dataclasses import asdict, dataclass
import re

from config.settings import settings

TIMELINE_VERSION = 'timeline-v7-bounded'


def rate_multiplier(rate):
    if not isinstance(rate, str) or not re.fullmatch(r'[+-]?\d+%', rate):
        raise ValueError('Tốc độ TTS phải có dạng +0% đến +30%.')
    value = 1 + int(rate[:-1])/100
    if value <= 0:
        raise ValueError('Tốc độ TTS không hợp lệ.')
    return value


@dataclass(frozen=True)
class TimingPolicy:
    max_speed: float
    lead: float
    pause: float
    borrow: float

    @classmethod
    def current(cls):
        return cls(settings.TTS_MAX_TOTAL_SPEED, settings.TTS_VOICE_LEAD_SECONDS,
                   settings.TTS_MIN_PAUSE_SECONDS, settings.TTS_MAX_BORROW_SECONDS)

    def identity(self):
        return {'version': TIMELINE_VERSION, **asdict(self)}

    def deadline(self, cues, position):
        cue = cues[position]
        if position+1 == len(cues):
            return cue.end
        return max(cue.end, min(cue.end+self.borrow, cues[position+1].start-self.pause))
