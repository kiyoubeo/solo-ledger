"""Unified Local & Google Cloud Storage (GCS) Backend Abstraction for SoloLedger."""

import hashlib
import hmac
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import httpx

from sololedger.config import settings

METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)


def normalize_storage_key(relative_key: str) -> str:
    """Normalize a relative archive object key and block path traversal."""
    raw = (relative_key or "").replace("\\", "/").strip().strip("/")
    if not raw:
        raise ValueError("Storage key must not be empty.")
    parts = [part for part in raw.split("/") if part]
    if not parts or any(part in (".", "..") for part in parts):
        raise ValueError(f"Invalid storage key (path traversal blocked): {relative_key}")
    return "/".join(parts)


class StorageBackend(ABC):
    """Abstract storage interface shared by local filesystem and Google Cloud Storage."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Return short backend identifier ('local' or 'gcs')."""

    @abstractmethod
    def put_bytes(
        self,
        relative_key: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Persist raw bytes at relative_key and return a storage path or URI."""

    @abstractmethod
    def get_bytes(self, relative_key: str) -> bytes:
        """Retrieve raw bytes stored at relative_key, raising FileNotFoundError if missing."""

    @abstractmethod
    def exists(self, relative_key: str) -> bool:
        """Return True if relative_key exists in the storage backend."""

    @abstractmethod
    def list_keys(self, prefix: str = "") -> List[str]:
        """List stored relative keys matching an optional prefix in sorted order."""

    @abstractmethod
    def build_download_url(self, relative_key: str, expires_in_sec: int = 3600) -> str:
        """Return a download URL or signed reference for relative_key."""


class LocalStorageBackend(StorageBackend):
    """Local filesystem storage backend anchored under archive_dir."""

    def __init__(self, archive_dir: Optional[Path] = None) -> None:
        self._archive_dir = Path(archive_dir) if archive_dir is not None else None

    @property
    def backend_name(self) -> str:
        return "local"

    @property
    def base_dir(self) -> Path:
        return self._archive_dir if self._archive_dir is not None else settings.archive_dir

    def _resolve_path(self, relative_key: str) -> Path:
        clean_key = normalize_storage_key(relative_key)
        root = self.base_dir.resolve()
        target = (root / clean_key).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"Storage key escapes archive root: {relative_key}")
        return target

    def put_bytes(
        self,
        relative_key: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        target = self._resolve_path(relative_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return str(target)

    def get_bytes(self, relative_key: str) -> bytes:
        target = self._resolve_path(relative_key)
        if not target.is_file():
            raise FileNotFoundError(f"Local archive file not found: {relative_key}")
        return target.read_bytes()

    def exists(self, relative_key: str) -> bool:
        return self._resolve_path(relative_key).is_file()

    def list_keys(self, prefix: str = "") -> List[str]:
        root = self.base_dir.resolve()
        if not root.exists():
            return []
        clean_prefix = (
            normalize_storage_key(prefix) if (prefix or "").replace("\\", "/").strip("/") else ""
        )
        keys: List[str] = []
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(root).as_posix()
            if not clean_prefix or rel.startswith(clean_prefix):
                keys.append(rel)
        return sorted(keys)

    def build_download_url(self, relative_key: str, expires_in_sec: int = 3600) -> str:
        clean_key = normalize_storage_key(relative_key)
        return f"/archive/{clean_key}"


class GcsStorageBackend(StorageBackend):
    """Lightweight Google Cloud Storage JSON API client via httpx."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "sololedger",
        access_token: Optional[str] = None,
        local_cache_dir: Optional[Path] = None,
        write_through_local: bool = True,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        clean_bucket = (bucket or "").strip()
        if not clean_bucket:
            raise ValueError("GCS bucket name is required for GcsStorageBackend.")
        self.bucket = clean_bucket
        self.prefix = (prefix or "").replace("\\", "/").strip().strip("/")
        self._explicit_token = (access_token or "").strip() or None
        self.local_cache_dir = Path(local_cache_dir) if local_cache_dir is not None else None
        self.write_through_local = write_through_local
        self.transport = transport
        self._cached_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def backend_name(self) -> str:
        return "gcs"

    def _object_name(self, relative_key: str) -> str:
        clean_key = normalize_storage_key(relative_key)
        return f"{self.prefix}/{clean_key}" if self.prefix else clean_key

    def _resolve_access_token(self) -> str:
        if self._explicit_token:
            return self._explicit_token
        if settings.gcs_access_token:
            return settings.gcs_access_token
        now = time.time()
        if self._cached_token and now < (self._token_expires_at - 30.0):
            return self._cached_token

        with httpx.Client(transport=self.transport, timeout=5.0) as client:
            resp = client.get(
                METADATA_TOKEN_URL,
                headers={"Metadata-Flavor": "Google"},
            )
            resp.raise_for_status()
            data = resp.json()

        token = str(data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("Failed to obtain OAuth2 access token from GCS metadata server.")
        expires_in = int(data.get("expires_in") or 3600)
        self._cached_token = token
        self._token_expires_at = now + expires_in
        return token

    def _auth_headers(self, extra: Optional[dict] = None) -> dict:
        headers = {"Authorization": f"Bearer {self._resolve_access_token()}"}
        if extra:
            headers.update(extra)
        return headers

    def put_bytes(
        self,
        relative_key: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        clean_key = normalize_storage_key(relative_key)
        obj_name = self._object_name(clean_key)
        url = (
            f"https://storage.googleapis.com/upload/storage/v1/b/"
            f"{quote(self.bucket, safe='')}/o"
            f"?uploadType=media&name={quote(obj_name, safe='')}"
        )
        with httpx.Client(transport=self.transport, timeout=30.0) as client:
            resp = client.post(
                url,
                headers=self._auth_headers({"Content-Type": content_type}),
                content=content,
            )
            resp.raise_for_status()

        if self.write_through_local:
            cache_root = (
                self.local_cache_dir if self.local_cache_dir is not None else settings.archive_dir
            )
            local_target = (cache_root.resolve() / clean_key).resolve()
            if local_target.is_relative_to(cache_root.resolve()):
                local_target.parent.mkdir(parents=True, exist_ok=True)
                local_target.write_bytes(content)

        return f"gs://{self.bucket}/{obj_name}"

    def get_bytes(self, relative_key: str) -> bytes:
        obj_name = self._object_name(relative_key)
        url = (
            f"https://storage.googleapis.com/storage/v1/b/"
            f"{quote(self.bucket, safe='')}/o/{quote(obj_name, safe='')}?alt=media"
        )
        with httpx.Client(transport=self.transport, timeout=30.0) as client:
            resp = client.get(url, headers=self._auth_headers())
            if resp.status_code == 404:
                raise FileNotFoundError(f"GCS object not found: gs://{self.bucket}/{obj_name}")
            resp.raise_for_status()
            return resp.content

    def exists(self, relative_key: str) -> bool:
        obj_name = self._object_name(relative_key)
        url = (
            f"https://storage.googleapis.com/storage/v1/b/"
            f"{quote(self.bucket, safe='')}/o/{quote(obj_name, safe='')}"
        )
        with httpx.Client(transport=self.transport, timeout=15.0) as client:
            resp = client.get(url, headers=self._auth_headers())
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            return True

    def list_keys(self, prefix: str = "") -> List[str]:
        clean_sub = (
            normalize_storage_key(prefix) if (prefix or "").replace("\\", "/").strip("/") else ""
        )
        if self.prefix and clean_sub:
            full_prefix = f"{self.prefix}/{clean_sub}"
        else:
            full_prefix = self.prefix or clean_sub

        base_url = (
            f"https://storage.googleapis.com/storage/v1/b/"
            f"{quote(self.bucket, safe='')}/o"
        )
        keys: List[str] = []
        page_token: Optional[str] = None
        strip_prefix = f"{self.prefix}/" if self.prefix else ""

        with httpx.Client(transport=self.transport, timeout=20.0) as client:
            while True:
                params = {}
                if full_prefix:
                    params["prefix"] = full_prefix
                if page_token:
                    params["pageToken"] = page_token
                resp = client.get(base_url, headers=self._auth_headers(), params=params)
                resp.raise_for_status()
                payload = resp.json()
                for item in payload.get("items") or []:
                    name = str(item.get("name") or "")
                    if not name:
                        continue
                    if strip_prefix and name.startswith(strip_prefix):
                        name = name[len(strip_prefix) :]
                    if name:
                        keys.append(name)
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break

        return sorted(set(keys))

    def build_download_url(self, relative_key: str, expires_in_sec: int = 3600) -> str:
        obj_name = self._object_name(relative_key)
        exp = int(time.time()) + max(1, int(expires_in_sec))
        payload = f"{self.bucket}/{obj_name}:{exp}"
        sig = hmac.new(
            settings.api_key.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"https://storage.googleapis.com/{self.bucket}/{obj_name}?expires={exp}&sig={sig}"


def get_storage_backend(
    backend_type: Optional[str] = None,
    archive_dir: Optional[Path] = None,
    bucket: Optional[str] = None,
    prefix: Optional[str] = None,
    access_token: Optional[str] = None,
    transport: Optional[httpx.BaseTransport] = None,
) -> StorageBackend:
    """Instantiate the configured StorageBackend ('local' or 'gcs')."""
    chosen = (backend_type or settings.storage_backend_type).strip().lower()
    if chosen == "gcs":
        return GcsStorageBackend(
            bucket=bucket or settings.gcs_bucket,
            prefix=settings.gcs_prefix if prefix is None else prefix,
            access_token=access_token,
            local_cache_dir=archive_dir,
            transport=transport,
        )
    return LocalStorageBackend(archive_dir=archive_dir)
