"""Pure services: generated images only, never import the application entry point."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
import importlib
import json
import random

import pytest
from PIL import Image


def storage():
    return importlib.import_module("app.services.screenshot_storage")


def contracts():
    return importlib.import_module("app.services.screenshot_contracts")


def image_bytes(size=(40, 60), *, fmt="PNG", color="white", noise=False):
    image = Image.new("RGB", size, color)
    if noise:
        image.frombytes(random.Random(1).randbytes(size[0] * size[1] * 3))
    output = BytesIO()
    image.save(output, format=fmt)
    return output.getvalue()


def upload(name="safe.png", data=None):
    return storage().UploadedImage(name, image_bytes() if data is None else data)


def test_random_paths_order_preserve_original_and_canonical_duplicates(tmp_path):
    s = storage()
    files = [upload("../../招募.png"), upload("two.jpg", image_bytes(fmt="JPEG", color="blue"))]
    assets = s.validate_and_store_images(files, root=tmp_path)
    assert [a.order for a in assets] == [0, 1]
    assert assets[0].original_filename == "招募.png"
    assert s.resolve_storage_path(tmp_path, assets[0].storage_key).read_bytes() == image_bytes()
    assert assets[0].storage_key != assets[1].storage_key
    assert "招募" not in assets[0].storage_key
    assert assets[0].width == 40 and assets[0].height == 60
    assert s.prepare_pieces(assets[0]) == list(assets[0].pieces)
    s.cleanup_assets(assets, root=tmp_path)
    assert not list(tmp_path.glob("*.png")) and not list(tmp_path.glob("*.jpg"))


@pytest.mark.parametrize("files,code", [
    ([], "image_count"),
    ([image_bytes()] * 6, "image_count"),
    ([b"not an image"], "invalid_image"),
])
def test_invalid_inputs_are_rejected_without_residue(tmp_path, files, code):
    s, c = storage(), contracts()
    with pytest.raises(c.ScreenshotError) as error:
        s.validate_and_store_images([upload(data=data) for data in files], root=tmp_path)
    assert error.value.code == code
    assert not [p for p in tmp_path.iterdir() if p.suffix != ".lock"]


def test_rejects_format_mismatch_and_animation(tmp_path):
    s, c = storage(), contracts()
    with pytest.raises(c.ScreenshotError, match="格式"):
        s.validate_and_store_images([upload("fake.png", image_bytes(fmt="JPEG"))], root=tmp_path)
    output = BytesIO()
    Image.new("RGB", (10, 10), "red").save(output, format="WEBP", save_all=True,
        append_images=[Image.new("RGB", (10, 10), "blue")], duration=100, loop=0)
    with pytest.raises(c.ScreenshotError) as error:
        s.validate_and_store_images([upload("animation.webp", output.getvalue())], root=tmp_path)
    assert error.value.code == "animated_image"


@pytest.mark.parametrize("changes,files,code", [
    ({"max_image_bytes": 10}, [image_bytes()], "image_too_large"),
    ({"max_batch_bytes": 100}, [image_bytes(), image_bytes(color="blue")], "batch_too_large"),
    ({"max_image_pixels": 10}, [image_bytes()], "image_pixels"),
    ({"max_batch_pixels": 3000}, [image_bytes(), image_bytes(color="blue")], "batch_pixels"),
    ({"max_dimension": 50}, [image_bytes()], "image_dimension"),
    ({"max_pieces": 1, "piece_height": 30, "vertical_overlap": 5}, [image_bytes()], "piece_budget"),
    ({"quota_bytes": 30}, [image_bytes()], "storage_quota"),
])
def test_limits_are_atomic(tmp_path, changes, files, code):
    s, c = storage(), contracts()
    with pytest.raises(c.ScreenshotError) as error:
        s.validate_and_store_images([upload(data=data) for data in files], root=tmp_path,
            limits=replace(c.DEFAULT_LIMITS, **changes))
    assert error.value.code == code
    assert not [p for p in tmp_path.iterdir() if p.suffix != ".lock"]


def test_duplicate_pixels_rollback_entire_batch(tmp_path):
    s, c = storage(), contracts()
    with pytest.raises(c.ScreenshotError) as error:
        s.validate_and_store_images([upload(), upload("other.png")], root=tmp_path)
    assert error.value.code == "duplicate_image"
    assert not list(tmp_path.glob("*.png"))


def test_tiling_keeps_tail_and_row_major_coordinates(tmp_path):
    s = storage()
    assets = s.validate_and_store_images([upload(data=image_bytes((1500, 3900)))], root=tmp_path)
    pieces = assets[0].pieces
    assert [(p.x, p.y) for p in pieces] == [(0, 0), (1280, 0), (0, 1840), (1280, 1840), (0, 3680), (1280, 3680)]
    assert max(p.y + p.height for p in pieces) == 3900
    assert all(p.width <= 1400 and p.height <= 2000 for p in pieces)
    assert all(p.base64_size <= 8_000_000 for p in pieces)


def test_exif_orientation_is_applied_only_to_prepared_pixels(tmp_path):
    s = storage()
    source = Image.new("RGB", (80, 20), "red")
    exif = source.getexif()
    exif[274] = 6
    output = BytesIO()
    source.save(output, format="JPEG", exif=exif)
    asset = s.validate_and_store_images([upload("rotated.jpg", output.getvalue())], root=tmp_path)[0]
    assert (asset.width, asset.height) == (20, 80)
    assert s.resolve_storage_path(tmp_path, asset.storage_key).read_bytes() == output.getvalue()
    with Image.open(s.resolve_storage_path(tmp_path, asset.pieces[0].storage_key)) as piece:
        assert piece.size == (20, 80)


def test_base64_budget_resplits_and_rechecks_count(tmp_path):
    s, c = storage(), contracts()
    limits = replace(c.DEFAULT_LIMITS, max_base64_bytes=18000)
    assets = s.validate_and_store_images([upload(data=image_bytes((150, 150), noise=True))], root=tmp_path, limits=limits)
    assert len(assets[0].pieces) > 1
    assert all(p.base64_size <= 18000 for p in assets[0].pieces)
    s.cleanup_assets(assets, root=tmp_path)
    with pytest.raises(c.ScreenshotError) as error:
        s.validate_and_store_images([upload(data=image_bytes((150, 150), noise=True))], root=tmp_path,
            limits=replace(limits, max_pieces=1))
    assert error.value.code == "piece_budget"


@pytest.mark.parametrize("key", ["../escape.png", "C:/windows/a.png", "foo.png", "a" * 32 + "/x.png"])
def test_private_reader_rejects_untrusted_keys(tmp_path, key):
    with pytest.raises(contracts().ScreenshotError):
        storage().resolve_storage_path(tmp_path, key)


def test_stream_is_read_in_bounded_chunks_and_quota_includes_history(tmp_path):
    s, c = storage(), contracts()
    class BoundedStream(BytesIO):
        def read(self, n=-1):
            assert 0 < n <= 65536
            return super().read(n)
    (tmp_path / "old.bin").write_bytes(b"a" * 100)
    with pytest.raises(c.ScreenshotError) as error:
        s.validate_and_store_images([upload(data=BoundedStream(image_bytes()))], root=tmp_path,
            limits=replace(c.DEFAULT_LIMITS, quota_bytes=150))
    assert error.value.code == "storage_quota"
    assert (tmp_path / "old.bin").read_bytes() == b"a" * 100


def test_contracts_strict_json_round_trip_and_schema_version():
    c = contracts()
    ref = c.EvidenceRef(1, 2, 0, 2, "本科", "ocr")
    field = c.ExtractedField("本科", "explicit", (ref,))
    facts = c.ScreenshotFacts({"education": field}, ({"job_title": field},), (), 1, (field,))
    encoded = facts.to_json()
    assert json.loads(encoded)["schema_version"] == 1
    assert c.ScreenshotFacts.from_json(encoded) == facts
    with pytest.raises(c.ScreenshotError):
        c.ScreenshotFacts.from_dict({**facts.to_dict(), "content_revision": "1"})
    with pytest.raises(c.ScreenshotError):
        c.ExtractedField.from_dict({**field.to_dict(), "state": "guessed"})
    with pytest.raises(c.ScreenshotError):
        c.EvidenceRef.from_dict({**ref.to_dict(), "text_end": -1})
    with pytest.raises(c.ScreenshotError):
        c.ScreenshotFacts.from_dict({**facts.to_dict(), "schema_version": 99})


def test_parallel_batches_cannot_both_consume_last_quota(tmp_path):
    s, c = storage(), contracts()
    probe = s.validate_and_store_images([upload()], root=tmp_path / "probe")
    one_batch_size = probe[0].byte_size + sum(p.byte_size for p in probe[0].pieces)
    limits = replace(c.DEFAULT_LIMITS, quota_bytes=one_batch_size + 1)
    destination = tmp_path / "shared"
    def store_batch():
        try:
            return s.validate_and_store_images([upload()], root=destination, limits=limits)
        except c.ScreenshotError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: store_batch(), range(2)))
    assert sum(isinstance(result, list) for result in results) == 1
    assert "storage_quota" in results
    assert sum(p.stat().st_size for p in destination.iterdir()) <= limits.quota_bytes


def test_read_failure_removes_only_its_own_files(tmp_path):
    s = storage()
    old_assets = s.validate_and_store_images([upload()], root=tmp_path)
    class BrokenStream(BytesIO):
        def read(self, n=-1):
            raise OSError("simulated interrupted upload")
    with pytest.raises(OSError):
        s.validate_and_store_images([upload("two.png", image_bytes(color="blue")),
            upload("broken.png", BrokenStream(b""))], root=tmp_path)
    assert s.resolve_storage_path(tmp_path, old_assets[0].storage_key).is_file()
    assert not list(tmp_path.glob("*.part"))
    assert len(list(tmp_path.glob("*.png"))) == 2


@pytest.mark.parametrize("value", [True, 2.0, "2", {}, None])
def test_contracts_reject_non_integer_versions(value):
    c = contracts()
    facts = c.ScreenshotFacts({}, (), (), 1).to_dict()
    with pytest.raises(c.ScreenshotError):
        c.ScreenshotFacts.from_dict({**facts, "content_revision": value})


def test_ocr_and_confirmation_contract_json_validation():
    c = contracts()
    ocr = c.OcrResult("文字", "model", "stop", 30, {"input_tokens": 10})
    assert c.OcrResult.from_json(ocr.to_json()) == ocr
    confirmation = c.ConfirmationResult(1, 2, "pending_verification", ())
    assert c.ConfirmationResult.from_json(confirmation.to_json()) == confirmation
    with pytest.raises(c.ScreenshotError):
        c.OcrResult.from_dict({**ocr.to_dict(), "usage": {"input_tokens": "10"}})
    with pytest.raises(c.ScreenshotError):
        c.ExtractedField.from_dict({"schema_version": 1, "value": "x" * 24001,
            "state": "explicit", "evidence_refs": []})


def test_no_payload_can_exceed_unsplittable_base64_budget(tmp_path):
    s, c = storage(), contracts()
    with pytest.raises(c.ScreenshotError) as error:
        s.validate_and_store_images([upload(data=image_bytes((1, 1)))], root=tmp_path,
            limits=replace(c.DEFAULT_LIMITS, max_base64_bytes=1))
    assert error.value.code == "base64_budget"
    assert not list(tmp_path.glob("*.png"))


def test_direct_contract_serialization_rejects_invalid_usage_types():
    c = contracts()
    with pytest.raises(c.ScreenshotError):
        c.OcrResult("文字", "model", "stop", 30, {"input_tokens": True}).to_json()


def test_python310_path_api_remains_supported(tmp_path, monkeypatch):
    monkeypatch.delattr(Path, "is_junction")
    assert storage().validate_and_store_images([upload()], root=tmp_path)


def test_upload_root_with_linked_ancestor_is_rejected(tmp_path, monkeypatch):
    directory = tmp_path / "linked" / "uploads"
    monkeypatch.setattr(Path, "is_junction", lambda path: path.name == "linked")
    with pytest.raises(contracts().ScreenshotError) as error:
        storage().validate_and_store_images([upload()], root=directory)
    assert error.value.code == "unsafe_path"
    assert not directory.exists()


def test_contract_json_rejects_duplicate_keys():
    c = contracts()
    encoded = '{"schema_version":1,"fields":{},"roles":[],"warnings":[],"content_revision":1,"content_revision":2}'
    with pytest.raises(c.ScreenshotError):
        c.ScreenshotFacts.from_json(encoded)
