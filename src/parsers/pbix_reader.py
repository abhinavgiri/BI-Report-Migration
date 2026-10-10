"""Read the raw parts of a Power BI .pbix file.

A .pbix is a ZIP (OPC) container. The only part we depend on is ``Report/Layout``,
which is the report definition stored as UTF-16LE JSON with no BOM. Everything
else (DataModel, DataMashup, Connections, ...) varies between files, so it is
exposed but never required.

This module knows nothing about visuals or Tableau. It exists so that no other
module ever has to touch ``zipfile`` or worry about text encodings.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path


class PbixError(Exception):
    """Raised when a file is not a usable .pbix."""


LAYOUT_PART = "Report/Layout"
DATA_MODEL_PART = "DataModel"
VERSION_PART = "Version"


def decode_text_part(raw: bytes) -> str:
    """Decode a text part, detecting the encoding from the leading bytes.

    Observed in real files: UTF-16LE without a BOM (first bytes ``7b 00``).
    A BOM-prefixed UTF-16 or plain UTF-8 part is handled too, so a future
    Power BI build that changes the encoding fails loudly in json.loads rather
    than silently mis-decoding.
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if len(raw) >= 2 and raw[1] == 0:
        return raw.decode("utf-16-le")
    return raw.decode("utf-8")


def load_json_string(value):
    """Decode Power BI's "JSON stored inside a JSON string" fields.

    ``config``, ``query``, ``filters`` and ``dataTransforms`` are JSON documents
    serialised as strings inside the layout JSON, so they need a second parse.
    Returns ``None`` for missing values and passes already-parsed objects through,
    so callers can use it on any field without checking its type first.
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise TypeError(f"cannot decode JSON from {type(value).__name__}")


class PbixReader:
    """Open a .pbix and expose its parts.

    >>> reader = PbixReader("report.pbix")
    >>> reader.layout()["sections"]
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise PbixError(f"file not found: {self.path}")
        if not zipfile.is_zipfile(self.path):
            raise PbixError(f"not a ZIP container, so not a .pbix: {self.path.name}")
        with zipfile.ZipFile(self.path) as zf:
            self.parts: frozenset[str] = frozenset(zf.namelist())
        if LAYOUT_PART not in self.parts:
            raise PbixError(
                f"{self.path.name} has no {LAYOUT_PART} part; "
                "is it a PBIR-format or template file?"
            )
        self._layout: dict | None = None

    def has_part(self, name: str) -> bool:
        return name in self.parts

    def read_bytes(self, name: str) -> bytes:
        if name not in self.parts:
            raise PbixError(f"{self.path.name} has no part named {name!r}")
        with zipfile.ZipFile(self.path) as zf:
            return zf.read(name)

    def read_text(self, name: str) -> str:
        return decode_text_part(self.read_bytes(name))

    def version(self) -> str | None:
        return self.read_text(VERSION_PART).strip() if self.has_part(VERSION_PART) else None

    def has_data_model(self) -> bool:
        return self.has_part(DATA_MODEL_PART)

    def layout(self) -> dict:
        """The decoded ``Report/Layout`` document (cached after first read)."""
        if self._layout is None:
            doc = json.loads(self.read_text(LAYOUT_PART))
            if not isinstance(doc, dict) or "sections" not in doc:
                raise PbixError(f"{LAYOUT_PART} in {self.path.name} has no 'sections'")
            self._layout = doc
        return self._layout

    def __repr__(self) -> str:
        return f"PbixReader({self.path.name!r}, parts={len(self.parts)})"
