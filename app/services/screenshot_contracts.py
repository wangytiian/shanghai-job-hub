"""Shared, versioned contracts for screenshot services; no database dependencies."""

from dataclasses import asdict, dataclass
import json
from typing import ClassVar

SCHEMA_VERSION = 1
PREPROCESSING_VERSION = "screenshot-preprocess-v1"
OCR_RULE_VERSION = "screenshot-ocr-v2-text-recognition"
EXTRACTION_RULE_VERSION = "screenshot-extraction-v3-guided-review"
TEMPLATE_VERSION = "screenshot-template-v1"
MAX_IMAGES = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_BATCH_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_BATCH_PIXELS = 80_000_000
MAX_DIMENSION = 30_000
MAX_PIECES = 24
MAX_BASE64_BYTES = 8_000_000
MAX_TEXT_CHARS = 24_000
EXTRACTION_CHUNK_CHARS = 5_000
EXTRACTION_CHUNK_OVERLAP = 200
MAX_EXTRACTION_CHUNKS = 5
UPLOAD_QUOTA_BYTES = 2 * 1024 * 1024 * 1024
MAX_REQUEST_BYTES = 26 * 1024 * 1024
OCR_CONNECT_TIMEOUT = 10
OCR_READ_TIMEOUT = 120
SCREENSHOT_LEASE_SECONDS = 180
SCREENSHOT_HEARTBEAT_SECONDS = 30
SCREENSHOT_MAX_ATTEMPTS = 2
SCREENSHOT_RETRY_SECONDS = 30


class ScreenshotError(ValueError):
    """Safe operational error: caller may expose message/code, never raw causes."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


@dataclass(frozen=True)
class ScreenshotLimits:
    max_images: int = MAX_IMAGES
    max_image_bytes: int = MAX_IMAGE_BYTES
    max_batch_bytes: int = MAX_BATCH_BYTES
    max_image_pixels: int = MAX_IMAGE_PIXELS
    max_batch_pixels: int = MAX_BATCH_PIXELS
    max_dimension: int = MAX_DIMENSION
    piece_width: int = 1400
    piece_height: int = 2000
    horizontal_overlap: int = 120
    vertical_overlap: int = 160
    max_pieces: int = MAX_PIECES
    max_base64_bytes: int = MAX_BASE64_BYTES
    quota_bytes: int = UPLOAD_QUOTA_BYTES

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value < (0 if "overlap" in name else 1):
                raise ValueError(f"Invalid screenshot limit: {name}")
        if self.horizontal_overlap >= self.piece_width or self.vertical_overlap >= self.piece_height:
            raise ValueError("Overlap must be smaller than piece dimensions")


DEFAULT_LIMITS = ScreenshotLimits()


def _invalid():
    raise ScreenshotError("invalid_contract", "截图数据结构或版本无效，请刷新后重试")


def _text(value, maximum=MAX_TEXT_CHARS):
    if not isinstance(value, str) or len(value) > maximum:
        _invalid()
    return value


def _integer(value, minimum=0, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not int or value < minimum:
        _invalid()
    return value


def _sequence(value, maximum=100):
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        _invalid()
    return value


class JsonContract:
    schema_version: ClassVar[int] = SCHEMA_VERSION

    def to_dict(self):
        def encode(value):
            if isinstance(value, JsonContract):
                return value.to_dict()
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [encode(item) for item in value]
            return value
        return {"schema_version": SCHEMA_VERSION, **{name: encode(getattr(self, name)) for name in self.__dataclass_fields__ if name != "schema_version"}}

    def to_json(self):
        # Validate even when the dataclass was instantiated directly by application code.
        data = self.to_dict()
        type(self).from_dict(data)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, value):
        if not isinstance(value, str) or len(value) > 2_000_000:
            _invalid()
        def unique_keys(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    _invalid()
                result[key] = item
            return result
        try:
            data = json.loads(value, object_pairs_hook=unique_keys)
        except (ValueError, TypeError):
            _invalid()
        return cls.from_dict(data)

    @classmethod
    def _check(cls, data):
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != SCHEMA_VERSION:
            _invalid()
        expected = set(cls.__dataclass_fields__) - {"schema_version"}
        if set(data) - expected - {"schema_version"}:
            _invalid()
        return data


@dataclass(frozen=True)
class EvidenceRef(JsonContract):
    asset_id: int | None
    piece_id: int | None
    text_start: int
    text_end: int
    quote: str
    origin: str

    @classmethod
    def from_dict(cls, data):
        d = cls._check(data)
        try:
            origin = d["origin"]
            start, end = _integer(d["text_start"]), _integer(d["text_end"])
            if origin not in ("ocr", "manual") or end < start:
                _invalid()
            return cls(_integer(d["asset_id"], 1, True), _integer(d["piece_id"], 1, True), start, end, _text(d["quote"]), origin)
        except (KeyError, TypeError):
            _invalid()


@dataclass(frozen=True)
class ExtractedField(JsonContract):
    value: str
    state: str
    evidence_refs: tuple[EvidenceRef, ...]

    @classmethod
    def from_dict(cls, data):
        d = cls._check(data)
        try:
            if d["state"] not in ("explicit", "missing", "unclear", "conflict"):
                _invalid()
            return cls(_text(d["value"]), d["state"], tuple(EvidenceRef.from_dict(ref) for ref in _sequence(d["evidence_refs"])))
        except (KeyError, TypeError):
            _invalid()


@dataclass(frozen=True)
class ScreenshotFacts(JsonContract):
    fields: dict[str, ExtractedField]
    roles: tuple[dict[str, ExtractedField], ...]
    warnings: tuple[str, ...]
    content_revision: int
    common_requirements: tuple[ExtractedField, ...] = ()

    @classmethod
    def from_dict(cls, data):
        d = cls._check(data)
        def field_map(value):
            if not isinstance(value, dict) or len(value) > 100:
                _invalid()
            return {_text(key, 100): ExtractedField.from_dict(item) for key, item in value.items()}
        try:
            return cls(field_map(d["fields"]), tuple(field_map(role) for role in _sequence(d["roles"])),
                tuple(_text(warning, 2000) for warning in _sequence(d["warnings"])), _integer(d["content_revision"], 1),
                tuple(ExtractedField.from_dict(item) for item in _sequence(d.get("common_requirements", []))))
        except (KeyError, TypeError):
            _invalid()


@dataclass(frozen=True)
class OcrResult(JsonContract):
    text: str
    model: str
    finish_reason: str
    elapsed_ms: int
    usage: dict[str, int] | None

    @classmethod
    def from_dict(cls, data):
        d = cls._check(data)
        try:
            usage = d["usage"]
            if usage is not None:
                if not isinstance(usage, dict) or len(usage) > 100:
                    _invalid()
                usage = {_text(key, 100): _integer(value) for key, value in usage.items()}
            return cls(_text(d["text"]), _text(d["model"], 200), _text(d["finish_reason"], 100), _integer(d["elapsed_ms"]), usage)
        except (KeyError, TypeError):
            _invalid()


@dataclass(frozen=True)
class ConfirmationResult(JsonContract):
    import_id: int
    job_id: int
    state: str
    distribution_ids: tuple[int, ...]

    @classmethod
    def from_dict(cls, data):
        d = cls._check(data)
        try:
            if d["state"] not in ("pending_verification", "pending_review", "publishable"):
                _invalid()
            return cls(_integer(d["import_id"], 1), _integer(d["job_id"], 1), d["state"], tuple(_integer(item, 1) for item in _sequence(d["distribution_ids"])))
        except (KeyError, TypeError):
            _invalid()
