"""Safe GGUF header metadata inspector and SHA-256 calculator.

Inspects GGUF v2/v3 headers without executing or loading tensors into memory so
corrupted files, renamed non-GGUF files, split multi-file GGUFs, and incompatible
model architectures are rejected with explicit diagnostic reasons.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_GGUF_MAGIC = b"GGUF"
_SPLIT_FILENAME_RE = re.compile(r"-\d{4,5}-of-\d{4,5}\.gguf$", re.IGNORECASE)
_SCALAR_FORMATS: dict[int, tuple[str, int]] = {
    0: ("<B", 1),   # uint8
    1: ("<b", 1),   # int8
    2: ("<H", 2),   # uint16
    3: ("<h", 2),   # int16
    4: ("<I", 4),   # uint32
    5: ("<i", 4),   # int32
    6: ("<f", 4),   # float32
    7: ("<?", 1),   # bool
    10: ("<Q", 8),  # uint64
    11: ("<q", 8),  # int64
    12: ("<d", 8),  # float64
}
_STRING_TYPE = 8
_ARRAY_TYPE = 9
_MAX_METADATA_STRING_BYTES = 1024 * 1024  # 1 MiB guard against malformed counts
_MAX_KV_COUNT = 65536


@dataclass
class GgufInspectionResult:
    file_path: str
    file_exists: bool
    file_size_bytes: int = 0
    file_mtime_ns: int = 0
    format_valid: bool = False
    is_split_gguf: bool = False
    gguf_version: int | None = None
    tensor_count: int = 0
    architecture: str | None = None
    model_name: str | None = None
    size_label: str | None = None
    file_type: int | None = None
    split_count: int | None = None
    split_no: int | None = None
    sha256: str = ""
    error: str | None = None


def compute_file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest using buffered streaming I/O."""
    if hasattr(hashlib, "file_digest"):
        with path.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest().lower()
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def _read_exact(f: Any, n: int) -> bytes:
    buf = f.read(n)
    if len(buf) != n:
        raise ValueError("Unexpected EOF while reading GGUF header")
    return buf


def _read_gguf_string(f: Any, *, store: bool = True) -> str:
    (n,) = struct.unpack("<Q", _read_exact(f, 8))
    if n > _MAX_METADATA_STRING_BYTES:
        raise ValueError(f"Invalid GGUF string length: {n}")
    raw = _read_exact(f, n)
    if not store:
        return ""
    return raw.decode("utf-8", errors="replace")


def _skip_or_read_value(f: Any, vtype: int, *, store: bool = True) -> Any:
    if vtype in _SCALAR_FORMATS:
        fmt, size = _SCALAR_FORMATS[vtype]
        raw = _read_exact(f, size)
        return struct.unpack(fmt, raw)[0] if store else None
    if vtype == _STRING_TYPE:
        return _read_gguf_string(f, store=store)
    if vtype == _ARRAY_TYPE:
        atype, alen = struct.unpack("<IQ", _read_exact(f, 12))
        if alen > 2_000_000:
            raise ValueError(f"Invalid GGUF array length: {alen}")
        # Do not materialize large vocab arrays into Python memory
        if not store or alen > 16:
            if atype in _SCALAR_FORMATS:
                _, elem_size = _SCALAR_FORMATS[atype]
                f.seek(elem_size * alen, os.SEEK_CUR)
            else:
                for _ in range(alen):
                    _skip_or_read_value(f, atype, store=False)
            return None
        return [_skip_or_read_value(f, atype, store=True) for _ in range(alen)]
    raise ValueError(f"Unsupported GGUF metadata value type: {vtype}")


def inspect_gguf_file(path: str | Path, *, compute_hash: bool = True) -> GgufInspectionResult:
    """Safely inspect GGUF metadata headers and optionally compute SHA-256."""
    resolved = Path(path)
    try:
        resolved = resolved.resolve()
    except OSError:
        pass

    if not resolved.exists():
        return GgufInspectionResult(
            file_path=str(resolved),
            file_exists=False,
            error="指定されたモデルファイルが見つかりません。",
        )
    if not resolved.is_file():
        return GgufInspectionResult(
            file_path=str(resolved),
            file_exists=False,
            error="指定されたパスは通常のファイルではありません。",
        )

    st = resolved.stat()
    size_bytes = int(st.st_size)
    mtime_ns = int(st.st_mtime_ns)
    split_by_name = bool(_SPLIT_FILENAME_RE.search(resolved.name))

    if size_bytes < 24:
        return GgufInspectionResult(
            file_path=str(resolved),
            file_exists=True,
            file_size_bytes=size_bytes,
            file_mtime_ns=mtime_ns,
            is_split_gguf=split_by_name,
            error="ファイルサイズが小さすぎるため有効なGGUFファイルではありません。",
        )

    kvs: dict[str, Any] = {}
    wanted_keys = {
        "general.architecture",
        "general.name",
        "general.size_label",
        "general.file_type",
        "split.count",
        "split.no",
    }
    try:
        with resolved.open("rb") as f:
            magic = _read_exact(f, 4)
            if magic != _GGUF_MAGIC:
                return GgufInspectionResult(
                    file_path=str(resolved),
                    file_exists=True,
                    file_size_bytes=size_bytes,
                    file_mtime_ns=mtime_ns,
                    is_split_gguf=split_by_name,
                    error="先頭マジックが GGUF ではありません（対応外のファイル形式または破損ファイル）。",
                )
            version, tensor_count, kv_count = struct.unpack("<IQQ", _read_exact(f, 20))
            if version not in (2, 3):
                return GgufInspectionResult(
                    file_path=str(resolved),
                    file_exists=True,
                    file_size_bytes=size_bytes,
                    file_mtime_ns=mtime_ns,
                    is_split_gguf=split_by_name,
                    gguf_version=version,
                    error=f"未対応の GGUF バージョン（v{version}）です（v2/v3 のみ対応）。",
                )
            if kv_count > _MAX_KV_COUNT or tensor_count == 0:
                return GgufInspectionResult(
                    file_path=str(resolved),
                    file_exists=True,
                    file_size_bytes=size_bytes,
                    file_mtime_ns=mtime_ns,
                    is_split_gguf=split_by_name,
                    gguf_version=version,
                    tensor_count=int(tensor_count),
                    error="GGUF ヘッダー（テンソル数またはメタデータ数）が不正または破損しています。",
                )
            for _ in range(kv_count):
                k = _read_gguf_string(f, store=True)
                (vt,) = struct.unpack("<I", _read_exact(f, 4))
                val = _skip_or_read_value(f, vt, store=(k in wanted_keys))
                if k in wanted_keys:
                    kvs[k] = val
    except (ValueError, struct.error, OSError) as exc:
        return GgufInspectionResult(
            file_path=str(resolved),
            file_exists=True,
            file_size_bytes=size_bytes,
            file_mtime_ns=mtime_ns,
            is_split_gguf=split_by_name,
            error=f"GGUF ヘッダー解析エラー（ファイル破損の可能性）: {exc}",
        )

    split_count_raw = kvs.get("split.count")
    split_count = int(split_count_raw) if isinstance(split_count_raw, int) else None
    split_no_raw = kvs.get("split.no")
    split_no = int(split_no_raw) if isinstance(split_no_raw, int) else None
    is_split = split_by_name or (split_count is not None and split_count > 1)

    sha_hex = compute_file_sha256(resolved) if compute_hash else ""
    arch_raw = kvs.get("general.architecture")
    name_raw = kvs.get("general.name")
    size_label_raw = kvs.get("general.size_label")
    ftype_raw = kvs.get("general.file_type")

    return GgufInspectionResult(
        file_path=str(resolved),
        file_exists=True,
        file_size_bytes=size_bytes,
        file_mtime_ns=mtime_ns,
        format_valid=True,
        is_split_gguf=is_split,
        gguf_version=version,
        tensor_count=int(tensor_count),
        architecture=str(arch_raw) if isinstance(arch_raw, str) else None,
        model_name=str(name_raw) if isinstance(name_raw, str) else None,
        size_label=str(size_label_raw) if isinstance(size_label_raw, str) else None,
        file_type=int(ftype_raw) if isinstance(ftype_raw, int) else None,
        split_count=split_count,
        split_no=split_no,
        sha256=sha_hex,
        error=None,
    )
