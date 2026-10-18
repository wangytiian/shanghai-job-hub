"""Bounded private screenshot storage. All output names are server generated.

The batch function prepares every piece before returning: callers must not queue
model work until it succeeds. Call cleanup_assets if a subsequent DB transaction
fails. Storage never opens a database or makes a model/network request.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import stat
import threading
from typing import BinaryIO, Iterable
from uuid import uuid4
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .screenshot_contracts import DEFAULT_LIMITS, ScreenshotError, ScreenshotLimits

_KEY = re.compile(r"[0-9a-f]{32}\.(?:png|jpg|webp)\Z")
_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}
_THREAD_LOCK = threading.RLock()
_READ_SIZE = 65536


@dataclass(frozen=True)
class UploadedImage:
    filename: str
    content: bytes | BinaryIO


@dataclass(frozen=True)
class PreparedPiece:
    piece_index: int
    x: int
    y: int
    width: int
    height: int
    storage_key: str
    pixel_sha256: str
    byte_size: int
    mime_type: str
    base64_size: int


@dataclass(frozen=True)
class PreparedAsset:
    order: int
    original_filename: str
    storage_key: str
    image_format: str
    mime_type: str
    width: int
    height: int
    byte_size: int
    original_sha256: str
    pixel_sha256: str
    pieces: tuple[PreparedPiece, ...]


def _fail(code, message):
    raise ScreenshotError(code, message)


def _root(root):
    path = Path(root).absolute()
    _check_directory_links(path)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _is_link(path):
    if path.is_symlink():
        return True
    junction = getattr(path, "is_junction", None)
    if junction is not None:
        return junction()
    # pathlib.is_junction was only added in 3.12; supported Python 3.10/3.11
    # still expose Windows reparse attributes through lstat.
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _check_directory_links(path):
    if any(_is_link(item) for item in (path, *path.parents)):
        _fail("unsafe_path", "截图存储目录不能经过链接目录")


def resolve_storage_path(root: Path, storage_key: str) -> Path:
    if not isinstance(storage_key, str) or not _KEY.fullmatch(storage_key):
        _fail("unsafe_path", "无效的截图文件标识")
    directory = Path(root).absolute()
    _check_directory_links(directory)
    directory = directory.resolve()
    candidate = directory / storage_key
    if _is_link(candidate) or candidate.resolve().parent != directory:
        _fail("unsafe_path", "无效的截图文件路径")
    if not candidate.is_file():
        _fail("image_not_found", "截图文件不存在，请联系管理员恢复材料")
    return candidate


@contextmanager
def _exclusive_upload(directory):
    # OS lock covers separate web/worker processes; thread lock covers same-
    # process upload threads on platforms whose file locks are process scoped.
    with _THREAD_LOCK:
        lock_path = directory / ".upload.lock"
        if _is_link(lock_path):
            _fail("unsafe_path", "截图存储锁文件无效")
        with lock_path.open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _directory_size(directory):
    size = 0
    for path in directory.rglob("*"):
        if _is_link(path):
            _fail("unsafe_path", "截图存储目录含有链接，请联系管理员检查")
        if path.is_file():
            size += path.stat().st_size
    return size


def _pixel_digest(image):
    digest = sha256(f"{image.width}:{image.height}:RGBA:".encode("ascii"))
    # Hash in stripes to avoid another whole-image allocation.
    for y in range(0, image.height, 128):
        with image.crop((0, y, image.width, min(image.height, y + 128))) as stripe:
            digest.update(stripe.tobytes())
    return digest.hexdigest()


def _positions(length, maximum, overlap):
    positions = [0]
    while positions[-1] + maximum < length:
        positions.append(positions[-1] + maximum - overlap)
    return positions


def _encoded_pieces(image, limits):
    def encode_box(box):
        with image.crop(box) as crop:
            output = BytesIO()
            crop.save(output, format="PNG")
            data = output.getvalue()
        b64_size = 4 * ((len(data) + 2) // 3)
        if b64_size <= limits.max_base64_bytes:
            yield box, data, b64_size
            return
        x, y, right, bottom = box
        width, height = right - x, bottom - y
        if width == height == 1:
            _fail("base64_budget", "图片分片仍超出识别载荷限制，请裁剪后重试")
        if height >= width and height > 1:
            overlap = min(limits.vertical_overlap, max(0, height // 4))
            middle = y + height // 2
            children = [(x, y, right, middle + overlap), (x, middle, right, bottom)]
        else:
            overlap = min(limits.horizontal_overlap, max(0, width // 4))
            middle = x + width // 2
            children = [(x, y, middle + overlap, bottom), (middle, y, right, bottom)]
        for child in children:
            yield from encode_box(child)
    for y in _positions(image.height, limits.piece_height, limits.vertical_overlap):
        for x in _positions(image.width, limits.piece_width, limits.horizontal_overlap):
            yield from encode_box((x, y, min(x + limits.piece_width, image.width), min(y + limits.piece_height, image.height)))


def validate_and_store_images(files: Iterable[UploadedImage], *, root: Path,
                              limits: ScreenshotLimits = DEFAULT_LIMITS) -> list[PreparedAsset]:
    uploads = []
    for item in files:
        uploads.append(item)
        if len(uploads) > limits.max_images:
            _fail("image_count", f"每批需要 1–{limits.max_images} 张截图")
    if not uploads:
        _fail("image_count", f"每批需要 1–{limits.max_images} 张截图")
    directory = _root(root)
    with _exclusive_upload(directory):
        used = _directory_size(directory)
        created = []
        total_bytes = total_pixels = total_pieces = 0
        seen_pixels = set()
        assets = []

        def write_bytes(data, suffix):
            nonlocal used
            if used + len(data) > limits.quota_bytes:
                _fail("storage_quota", "截图目录已达到配额，请管理员备份清理后重试")
            key = f"{uuid4().hex}.{suffix}"
            path = directory / key
            with path.open("xb") as handle:
                created.append(path)
                handle.write(data)
            used += len(data)
            return key

        try:
            if used >= limits.quota_bytes:
                _fail("storage_quota", "截图目录已达到配额，请管理员备份清理后重试")
            for order, upload in enumerate(uploads):
                if not isinstance(upload, UploadedImage) or not isinstance(upload.filename, str):
                    _fail("invalid_image", "截图输入无效")
                filename = upload.filename.replace("\\", "/").rsplit("/", 1)[-1][:255]
                extension = Path(filename).suffix.lower()
                if extension not in (".png", ".jpg", ".jpeg", ".webp"):
                    _fail("invalid_image", "仅支持 PNG、JPEG、静态 WebP 格式")
                stream = BytesIO(upload.content) if isinstance(upload.content, bytes) else upload.content
                if not callable(getattr(stream, "read", None)):
                    _fail("invalid_image", "截图输入无效")
                temp_path = directory / f"{uuid4().hex}.part"
                image_bytes = 0
                original_hash = sha256()
                with temp_path.open("xb") as handle:
                    created.append(temp_path)
                    while True:
                        chunk = stream.read(_READ_SIZE)
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            _fail("invalid_image", "截图输入无效")
                        image_bytes += len(chunk)
                        total_bytes += len(chunk)
                        if image_bytes > limits.max_image_bytes:
                            _fail("image_too_large", "单张截图不能超过 10 MiB，请压缩或裁剪")
                        if total_bytes > limits.max_batch_bytes:
                            _fail("batch_too_large", "整批截图不能超过 25 MiB，请拆批")
                        if used + len(chunk) > limits.quota_bytes:
                            _fail("storage_quota", "截图目录已达到配额，请管理员备份清理后重试")
                        handle.write(chunk)
                        original_hash.update(chunk)
                        used += len(chunk)
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", Image.DecompressionBombWarning)
                        with Image.open(temp_path) as source:
                            fmt = source.format
                            if fmt not in _FORMATS or extension not in ({".jpg", ".jpeg"} if fmt == "JPEG" else {"." + _FORMATS[fmt][0]}):
                                _fail("invalid_image", "文件扩展名与实际图片格式不一致")
                            if getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) > 1:
                                _fail("animated_image", "不支持动画图片，请上传静态截图")
                            width, height = source.size
                            pixels = width * height
                            if max(width, height) > limits.max_dimension:
                                _fail("image_dimension", "截图单边超过像素限制，请拆图")
                            if pixels > limits.max_image_pixels:
                                _fail("image_pixels", "单张截图超过像素限制，请拆图")
                            total_pixels += pixels
                            if total_pixels > limits.max_batch_pixels:
                                _fail("batch_pixels", "整批截图超过像素限制，请拆批")
                            source.verify()
                        with Image.open(temp_path) as source:
                            source.load()
                            with ImageOps.exif_transpose(source) as oriented:
                                image = oriented.convert("RGBA")
                        with image:
                            pixel_hash = _pixel_digest(image)
                            if pixel_hash in seen_pixels:
                                _fail("duplicate_image", "同批已有相同截图，请删除重复图片")
                            seen_pixels.add(pixel_hash)
                            ext, mime = _FORMATS[fmt]
                            key = f"{uuid4().hex}.{ext}"
                            final_path = directory / key
                            temp_path.rename(final_path)
                            created.append(final_path)
                            pieces = []
                            for box, data, base64_size in _encoded_pieces(image, limits):
                                total_pieces += 1
                                if total_pieces > limits.max_pieces:
                                    _fail("piece_budget", "整批截图分片超过 24 片，请拆批或裁剪")
                                x, y, right, bottom = box
                                piece_key = write_bytes(data, "png")
                                with image.crop(box) as crop:
                                    digest = _pixel_digest(crop)
                                pieces.append(PreparedPiece(0, x, y, right-x, bottom-y, piece_key, digest, len(data), "image/png", base64_size))
                            pieces.sort(key=lambda piece: (piece.y, piece.x))
                            pieces = [PreparedPiece(index, p.x, p.y, p.width, p.height, p.storage_key, p.pixel_sha256, p.byte_size, p.mime_type, p.base64_size) for index, p in enumerate(pieces)]
                            assets.append(PreparedAsset(order, filename, key, fmt, mime, image.width, image.height,
                                image_bytes, original_hash.hexdigest(), pixel_hash, tuple(pieces)))
                except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                    raise ScreenshotError("invalid_image", "截图损坏或无法安全解码，请重新保存图片") from exc
            return assets
        except BaseException:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise


def prepare_pieces(asset: PreparedAsset, *, limits: ScreenshotLimits = DEFAULT_LIMITS) -> list[PreparedPiece]:
    """Return pieces prepared atomically during upload (no later unsafe phase)."""
    pieces = list(asset.pieces)
    if len(pieces) > limits.max_pieces:
        _fail("piece_budget", "截图分片超过限制，请拆批")
    if any(p.width > limits.piece_width or p.height > limits.piece_height or p.base64_size > limits.max_base64_bytes for p in pieces):
        _fail("base64_budget", "截图分片超出当前识别限制，请重新上传")
    return pieces


def cleanup_assets(assets: Iterable[PreparedAsset], *, root: Path) -> None:
    """Compensate only this batch's generated keys after a DB rollback."""
    directory = _root(root)
    with _exclusive_upload(directory):
        for asset in assets:
            for key in (asset.storage_key, *(piece.storage_key for piece in asset.pieces)):
                try:
                    resolve_storage_path(directory, key).unlink()
                except ScreenshotError as exc:
                    if exc.code != "image_not_found":
                        raise
