"""Tests for document parsers."""
from __future__ import annotations

import pytest
from pathlib import Path

from rfp_responder.parsers import parse_file, _parse_text

FIXTURES = Path(__file__).parent / "fixtures"


class TestTextParser:
    def test_parse_txt(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("Hello world. This is a test.", encoding="utf-8")
        result = parse_file(f)
        assert len(result) == 1
        assert "Hello world" in result[0].text
        assert result[0].source == "test.txt"

    def test_empty_txt_returns_nothing(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        result = parse_file(f)
        assert result == []

    def test_md_parsed_as_text(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Heading\n\nSome content here.", encoding="utf-8")
        result = parse_file(f)
        assert len(result) == 1

    def test_unsupported_extension_skipped(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"\x00\x01\x02")
        result = parse_file(f)
        assert result == []

    def test_corrupted_file_skipped_gracefully(self, tmp_path):
        # Write invalid bytes to a .pdf file — parser should log and return []
        f = tmp_path / "corrupt.pdf"
        f.write_bytes(b"not a real pdf")
        result = parse_file(f)
        assert result == []  # Error caught, not raised


class TestFixtureFile:
    def test_sample_txt_fixture(self):
        txt = FIXTURES / "sample.txt"
        if not txt.exists():
            pytest.skip("Fixture not created yet — run create_fixtures.py")
        result = parse_file(txt)
        assert len(result) == 1
        assert len(result[0].text) > 50
