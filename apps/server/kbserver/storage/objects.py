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

    # ---- 上传 staging（docs/13 §6.2）：分块续传的临时输入，完成后原子收纳 ----

    def audio_upload_dir(self) -> Path:
        d = self.tmp_dir / "audio-uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def new_staging_path(self) -> str:
        """生成受控 staging 文件名（不使用用户路径/文件名）。"""
        return f"up-{os.urandom(16).hex()}.part"

    def staging_file(self, name: str) -> Path:
        if "/" in name or "\\" in name or ".." in name or not name:
            raise ValueError(f"非法 staging 名：{name!r}")
        return self.audio_upload_dir() / name

    def append_staging(self, name: str, data: bytes) -> int:
        """向 staging 追加已校验块；返回写入后的字节数。"""
        path = self.staging_file(name)
        with open(path, "ab") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        return path.stat().st_size

    def truncate_staging(self, name: str, size: int) -> None:
        """按最后确认 offset 截去崩溃留下的未提交尾部。"""
        path = self.staging_file(name)
        if not path.exists():
            return
        with open(path, "r+b") as f:
            f.truncate(size)

    def discard_staging(self, name: str) -> None:
        try:
            self.staging_file(name).unlink()
        except (OSError, ValueError):
            pass

    def adopt_staging(self, name: str, sha256_hex: str) -> tuple[str, str, int]:
        """把同卷 staging 文件原子收纳为不可变对象（不再复制整份原件）。

        返回 (sha256, storage_key, bytes)。目标已存在（同内容）时丢弃 staging。
        """
        src = self.staging_file(name)
        if not src.exists():
            raise FileNotFoundError(f"staging 文件缺失：{name}")
        size = src.stat().st_size
        key = self.storage_key(sha256_hex)
        final = self.object_path(key)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            try:
                src.unlink()
            except OSError:
                pass
            return sha256_hex, key, size
        try:
            os.replace(src, final)
        except OSError:
            # 跨卷回退：流式复制后删除 staging（部署同卷时不会走到这里）
            with open(src, "rb") as f_in, open(final, "wb") as f_out:
                for block in iter(lambda: f_in.read(1024 * 1024), b""):
                    f_out.write(block)
                f_out.flush()
                os.fsync(f_out.fileno())
            try:
                src.unlink()
            except OSError:
                pass
        return sha256_hex, key, size

    def delete_object(self, storage_key: str) -> bool:
        """删除不可变对象；只接受存储 key，不接受任意路径。返回是否真的删除了文件。"""
        path = self.object_path(storage_key)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


class PayloadTooLarge(Exception):
    pass
