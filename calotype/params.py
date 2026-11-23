"""Screenshot request parameters and their limits."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_WIDTH = 3840
MAX_HEIGHT = 2160
MIN_SIDE = 100
MAX_FULL_PAGE_HEIGHT = 20_000
MAX_DELAY_MS = 10_000
# Upper bound on output pixels (width x height x scale^2), to keep memory sane.
MAX_OUTPUT_PIXELS = 80_000_000
# The WebP format cannot store an image taller or wider than this.
WEBP_MAX_SIDE = 16_383

Format = Literal["png", "jpeg", "webp"]
WaitUntil = Literal["load", "domcontentloaded", "networkidle", "commit"]

CONTENT_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}


class ScreenshotParams(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=2048)
    width: int = Field(1280, ge=MIN_SIDE, le=MAX_WIDTH)
    height: int = Field(800, ge=MIN_SIDE, le=MAX_HEIGHT)
    full_page: bool = False
    # The top of the one screen to capture, in CSS pixels from the top of the page.
    # Past the last screen it is moved up to the last screen (Renderer._scroll_to).
    scroll_y: int = Field(0, ge=0, le=MAX_FULL_PAGE_HEIGHT)
    format: Format = "png"
    quality: int | None = Field(None, ge=1, le=100)
    device_scale_factor: float = Field(1.0, ge=1.0, le=3.0)
    delay_ms: int = Field(0, ge=0, le=MAX_DELAY_MS)
    wait_until: WaitUntil = "load"
    block_ads: bool = False
    dark_mode: bool = False

    @field_validator("format", mode="before")
    @classmethod
    def _format_alias(cls, v):
        if isinstance(v, str) and v.lower() == "jpg":
            return "jpeg"
        return v.lower() if isinstance(v, str) else v

    def effective_quality(self) -> int | None:
        if self.format == "png":
            return None
        return self.quality if self.quality is not None else 80

    def check_combinations(self) -> str | None:
        """Cross-field rules. Returns a message, or None if fine."""
        if self.quality is not None and self.format == "png":
            return "quality applies only to jpeg and webp."
        if self.scroll_y and self.full_page:
            return "scroll_y picks one screen of the page, so it cannot be used with full_page=true. Send one or the other."
        pixels = self.width * self.height * self.device_scale_factor**2
        if pixels > MAX_OUTPUT_PIXELS:
            return "width x height x device_scale_factor^2 is too large."
        return None

    def full_page_height_cap(self) -> int:
        """Tallest full-page capture allowed for these settings, in CSS pixels."""
        by_pixels = int(MAX_OUTPUT_PIXELS / (self.width * self.device_scale_factor**2))
        cap = min(MAX_FULL_PAGE_HEIGHT, by_pixels)
        if self.format == "webp":
            cap = min(cap, int(WEBP_MAX_SIDE / self.device_scale_factor))
        return max(self.height, cap)
