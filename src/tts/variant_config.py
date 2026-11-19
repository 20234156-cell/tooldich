"""Validated settings for budgeted speech, independent of language and project paths."""
from pydantic import BaseModel, ConfigDict, Field, model_validator


class VariantTimingConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)

    strict_seconds: float = Field(default=.04, ge=0, le=1.5)
    max_drift_seconds: float = Field(default=1.5, ge=0, le=1.5)
    mean_drift_seconds: float = Field(default=.3, ge=0, le=.3)
    max_speed: float = Field(default=1.3, ge=1, le=1.3)
    pause_seconds: float = Field(default=.04, ge=0)
    translation_levels: int = Field(default=4, ge=1, le=4)
    pilot_units: int = Field(default=43, ge=1)

    @model_validator(mode='after')
    def validate_limits(self):
        if max(self.strict_seconds,self.mean_drift_seconds) > self.max_drift_seconds:
            raise ValueError('Strict and mean drift must be within the max drift')
        return self
