"""不可变对象存储与原子写（docs/02 §7.4）。

- 文件先写同卷临时路径，flush/fsync、验证摘要后 rename 到不可变对象位置。
- 对象按内容寻址：objects/<sha256 前 2 位>/<sha256>。
- 数据库提交前崩溃留下的临时文件视为孤儿，由清理任务回收。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import get_settings


class ObjectStore:
    def __init__(self, objects_dir: Path | None = None, tmp_dir: Path | None = None):
        settings = get_settings()
        self.objects_dir = Path(objects_dir) if objects_dir else settings.objects_dir
        self.tmp_dir = Path(tmp_dir) if tmp_dir else settings.tmp_dir
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def storage_key(self, sha256_hex: str) -> str:
        return f"{sha256_hex[:2]}/{sha256_hex}"

    def object_path(self, storage_key: str) -> Path:
        # 防路径逃逸：只允许 [0-9a-f]/[0-9a-f]{62,64} 形态
        parts = storage_key.split("/")
        if len(parts) != 2 or not all(p and all(c in "0123456789abcdef" for c in p) for p in parts):
            raise ValueError(f"非法 storage_key：{storage_key!r}")
        return self.objects_dir / parts[0] / parts[1]

    def put_bytes(self, data: bytes) -> tuple[str, str, int]:
        """写入字节，返回 (sha256, storage_key, bytes)。同内容幂等。"""
        sha = hashlib.sha256(data).hexdigest()
        key = self.storage_key(sha)
        final = self.object_path(key)
        if final.exists():
            return sha, key, len(data)
        self._atomic_write(final, data)
        return sha, key, len(data)

    def put_stream(self, stream, max_bytes: int) -> tuple[str, str, int]:
        """流式写入（上传大文件），超限抛 PAYLOAD_TOO_LARGE。"""
        h = hashlib.sha256()
        tmp_fd, tmp_name = tempfile.mkstemp(dir=self.tmp_dir, suffix=".upload")
        size = 0
        try:
            with os.fdopen(tmp_fd, "wb") as out:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise PayloadTooLarge(f"单文件上限 {max_bytes} 字节")
                    h.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha = h.hexdigest()
            key = self.storage_key(sha)
            final = self.object_path(key)
            if final.exists():
                os.unlink(tmp_name)
            else:
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_name, final)
            return sha, key, size
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _atomic_write(self, final: Path, data: bytes) -> None:
        tmp_fd, tmp_name = tempfile.mkstemp(dir=self.tmp_dir, suffix=".obj")
        try:
            with os.fdopen(tmp_fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            verify = hashlib.sha256(data).hexdigest()
            written = hashlib.sha256(Path(tmp_name).read_bytes()).hexdigest()
            if verify != written:
                raise RuntimeError("对象写入校验失败")
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_name, final)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def open_object(self, storage_key: str):
        return self.object_path(storage_key).open("rb")

    def read_object(self, storage_key: str) -> bytes:
        return self.object_path(storage_key).read_bytes()

    def object_exists(self, storage_key: str) -> bool:
        return self.object_path(storage_key).exists()


class PayloadTooLarge(Exception):
    pass
