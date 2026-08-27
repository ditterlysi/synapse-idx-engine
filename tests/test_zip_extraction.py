from __future__ import annotations

import zipfile

import pytest

from idx_digest.config import Settings
from idx_digest.extractors import extract_document


def _settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


def test_zip_extracts_supported_members_without_extractall(tmp_path) -> None:
    archive_path = tmp_path / "idx-attachment.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("docs/disclosure.txt", "Material disclosure body")
        archive.writestr("docs/data.json", '{"value": 123}')
        archive.writestr("docs/ignored.bin", b"not a supported document")

    result = extract_document(archive_path, "application/zip", _settings(tmp_path))

    assert "Material disclosure body" in result.text
    assert '"value": 123' in result.text
    assert "ignored.bin" not in result.text
    assert result.method == "zip[text]"


def test_zip_rejects_path_traversal_member(tmp_path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../outside.txt", "must never be extracted")

    with pytest.raises(ValueError, match="Unsafe ZIP member path"):
        extract_document(archive_path, "application/zip", _settings(tmp_path))

    assert not (tmp_path.parent / "outside.txt").exists()


def test_zip_requires_at_least_one_supported_document(tmp_path) -> None:
    archive_path = tmp_path / "unsupported.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("payload.bin", b"opaque payload")

    with pytest.raises(ValueError, match="contains no supported document attachments"):
        extract_document(archive_path, "application/zip", _settings(tmp_path))
