"""Storage abstraction for the Duke MJE platform.

Local mode wraps the existing UPLOAD_DIR / APP_OUTPUT_DIR layout. Azure Blob
mode is selected with STORAGE_BACKEND=azure_blob.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import threading
from abc import ABC, abstractmethod
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse


_bearer_token_var: ContextVar[str] = ContextVar("fm_bearer_token", default="")
_tenant_id_var: ContextVar[str] = ContextVar("fm_tenant_id", default="")
_correlation_id_var: ContextVar[str] = ContextVar("fm_correlation_id", default="")
_cookie_var: ContextVar[str] = ContextVar("fm_cookie", default="")


def set_fm_auth(
    bearer: str = "", tenant_id: str = "", correlation_id: str = "", cookie: str = ""
) -> None:
    """Set request-scoped File Manager credentials for downstream calls."""
    _bearer_token_var.set(bearer)
    _tenant_id_var.set(tenant_id)
    _correlation_id_var.set(correlation_id)
    _cookie_var.set(cookie)


def get_fm_auth() -> dict[str, str]:
    """Return the request-scoped File Manager credentials."""
    return {
        "bearer": _bearer_token_var.get(),
        "tenant_id": _tenant_id_var.get(),
        "correlation_id": _correlation_id_var.get(),
        "cookie": _cookie_var.get(),
    }


def is_blob_uri(locator: str) -> bool:
    """Detect whether a locator points to remote blob storage."""
    if not locator:
        return False
    s = locator.lower()
    return (
        s.startswith("azure://")
        or s.startswith("blob://")
        or (s.startswith("https://") and ".blob.core.windows.net" in s)
    )


def is_fm_url(locator: str) -> bool:
    """Detect File Manager HTTP URLs (excluding Azure Blob URLs)."""
    if not locator:
        return False
    s = locator.lower()
    return (s.startswith("http://") or s.startswith("https://")) and (
        ".blob.core.windows.net" not in s
    )


def _fm_filename_hint(url: str) -> str:
    """Derive a filename from a File Manager download URL."""
    parsed = urlparse(url)
    tail = (parsed.path or "").rstrip("/").rsplit("/", 1)[-1]
    if tail in ("download", "download-url"):
        parts = (parsed.path or "").rstrip("/").split("/")
        tail = parts[-2] if len(parts) >= 2 else ""
    return tail or "fmfile"


def _fm_internal_auth_headers() -> dict[str, str]:
    """Build authentication headers for File Manager calls."""
    headers: dict[str, str] = {}
    bearer = _bearer_token_var.get()
    if bearer:
        headers["Authorization"] = (
            bearer if bearer.startswith("Bearer ") else f"Bearer {bearer}"
        )
    tenant = _tenant_id_var.get()
    if tenant:
        headers["X-Tenant-Id"] = tenant
    cookie = _cookie_var.get()
    if cookie:
        headers["Cookie"] = cookie
    if "Authorization" not in headers:
        key = os.getenv("INTERNAL_SERVICE_KEY", "")
        if key:
            try:
                from .internal_auth import compute_internal_auth_header

                headers["x-internal-auth"] = compute_internal_auth_header(key)
            except (ImportError, AttributeError):
                pass
    corr = _correlation_id_var.get()
    if corr:
        headers["X-Correlation-Id"] = corr
    return headers


def _fm_original_filename(url: str, timeout_s: float = 10.0) -> str:
    """Query File Manager metadata for the original filename."""
    import httpx

    parsed = urlparse(url)
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/download") or path.endswith("/download-url"):
        meta_path = path.rsplit("/", 1)[0]
    else:
        return ""
    meta_url = f"{parsed.scheme}://{parsed.netloc}{meta_path}"
    try:
        response = httpx.get(
            meta_url, headers=_fm_internal_auth_headers(), timeout=timeout_s
        )
        if response.status_code == 200:
            return response.json().get("originalFileName", "") or ""
    except Exception:
        pass
    return ""


_MIME_TO_EXT = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "text/csv": ".csv",
    "application/pdf": ".pdf",
    "application/json": ".json",
}


def _filename_from_response(response: object) -> str:
    """Extract a filename from Content-Disposition if present."""
    import re

    cd = response.headers.get("content-disposition", "")  # type: ignore[attr-defined]
    if cd:
        match = re.search(
            r"filename\*?=(?:[\w-]+'')?[\"']?([^\"';]+)", cd, re.I
        )
        if match:
            return match.group(1).strip()
    return ""


def _ext_from_content_type(response: object) -> str:
    content_type = (
        response.headers.get("content-type", "").split(";")[0].strip().lower()  # type: ignore[attr-defined]
    )
    return _MIME_TO_EXT.get(content_type, "")


def fm_url_exists(url: str, timeout_s: float = 10.0) -> bool:
    """HEAD a File Manager URL and report whether it returned 200."""
    import httpx

    try:
        response = httpx.head(
            url,
            headers=_fm_internal_auth_headers(),
            timeout=timeout_s,
            follow_redirects=True,
        )
        return response.status_code == 200
    except Exception:
        return False


def fm_download_to(
    url: str,
    dest_dir: Path,
    timeout_s: float = 120.0,
    *,
    filename_hint: str = "",
) -> Path:
    """Download a File Manager URL to a process-local cache."""
    import hashlib
    import httpx

    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    hint = filename_hint.strip() if filename_hint else ""
    if not hint:
        hint = _fm_original_filename(url) or _fm_filename_hint(url)
    hint = Path(hint).name
    cached = Path(dest_dir) / "fm" / f"{cache_key}_{hint}"
    if cached.exists():
        return cached
    cached.parent.mkdir(parents=True, exist_ok=True)
    headers = _fm_internal_auth_headers()
    with httpx.stream("GET", url, headers=headers, timeout=timeout_s) as response:
        response.raise_for_status()
        if "." not in hint:
            actual_name = _filename_from_response(response)
            if actual_name:
                cached = cached.with_name(f"{cache_key}_{Path(actual_name).name}")
            else:
                ext = _ext_from_content_type(response)
                if ext:
                    cached = cached.with_name(cached.name + ext)
        with cached.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)
    return cached


def fm_upload_to(
    local_path: Path | str,
    fm_base_url: str,
    *,
    usecase_id: str = "",
    timeout_s: float = 60.0,
) -> str:
    """Upload a local file to File Manager and return its download URL."""
    import httpx

    local_path = Path(local_path)
    headers = _fm_internal_auth_headers()
    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    form_data: dict[str, str] = {}
    if usecase_id:
        form_data["useCaseId"] = usecase_id
    with local_path.open("rb") as handle:
        response = httpx.post(
            fm_base_url,
            files={"file": (local_path.name, handle, media_type)},
            data=form_data,
            headers=headers,
            timeout=timeout_s,
        )
    response.raise_for_status()
    body = response.json()
    file_uid = body.get("uid") or body.get("fileUid") or body.get("id") or ""
    download_url = body.get("downloadUrl") or body.get("download_url") or ""
    if not download_url and file_uid:
        base = fm_base_url.split("?", 1)[0].rstrip("/")
        download_url = f"{base}/{file_uid}/download"
    return download_url


class StorageBackend(ABC):
    """Common interface for input and output storage backends."""

    @abstractmethod
    def save_upload(
        self, src: BinaryIO, *, key: str, content_type: str | None = None
    ) -> str:
        """Persist an uploaded byte stream and return its locator."""

    @abstractmethod
    def open_read(self, locator: str) -> BinaryIO:
        """Open a stored file for reading. Caller must close."""

    @abstractmethod
    def ensure_local(self, locator: str, *, filename_hint: str = "") -> Path:
        """Materialize the locator on local disk and return its path."""

    @abstractmethod
    def publish_output(self, local_path: Path | str, *, key: str) -> str:
        """Publish a generated file and return its canonical locator."""

    @abstractmethod
    def exists(self, locator: str) -> bool: ...

    @abstractmethod
    def delete(self, locator: str) -> None: ...

    @abstractmethod
    def signed_url(self, locator: str, ttl_seconds: int = 900) -> str: ...

    @abstractmethod
    def size(self, locator: str) -> int: ...


class LocalFsBackend(StorageBackend):
    """Wrap the existing UPLOAD_DIR / APP_OUTPUT_DIR layout."""

    def __init__(self, upload_dir: Path, output_dir: Path) -> None:
        self.upload_dir = Path(upload_dir).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_key(self, key: str, *, output: bool = False) -> Path:
        if not key:
            raise ValueError("storage key must not be empty")
        path = Path(key)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"invalid storage key: {key!r}")
        base = self.output_dir if output else self.upload_dir
        resolved = (base / key).resolve()
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"storage key {key!r} escapes {base}") from exc
        return resolved

    def save_upload(
        self, src: BinaryIO, *, key: str, content_type: str | None = None
    ) -> str:
        dest = self._resolve_key(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            shutil.copyfileobj(src, handle)
        return str(dest)

    def open_read(self, locator: str) -> BinaryIO:
        if is_fm_url(locator):
            return fm_download_to(locator, self.upload_dir).open("rb")
        return self.ensure_local(locator).open("rb")

    def ensure_local(self, locator: str, *, filename_hint: str = "") -> Path:
        if not locator:
            return Path(locator)
        if is_fm_url(locator):
            return fm_download_to(locator, self.upload_dir, filename_hint=filename_hint)
        path = Path(locator)
        if path.is_absolute() and path.exists():
            return path
        for base in (self.upload_dir, self.output_dir):
            candidate = (base / locator).resolve()
            if candidate.exists():
                return candidate
        return path

    def publish_output(self, local_path: Path | str, *, key: str) -> str:
        source = Path(local_path).resolve()
        try:
            source.relative_to(self.output_dir)
            return str(source)
        except ValueError:
            dest = self._resolve_key(key, output=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source != dest:
                shutil.copy2(source, dest)
            return str(dest)

    def exists(self, locator: str) -> bool:
        if is_fm_url(locator):
            return fm_url_exists(locator)
        return self.ensure_local(locator).exists()

    def delete(self, locator: str) -> None:
        if is_fm_url(locator):
            return
        path = self.ensure_local(locator)
        if path.exists():
            path.unlink()

    def signed_url(self, locator: str, ttl_seconds: int = 900) -> str:
        if is_fm_url(locator):
            return locator
        return str(self.ensure_local(locator))

    def size(self, locator: str) -> int:
        return self.ensure_local(locator).stat().st_size


class AzureBlobBackend(StorageBackend):
    """Azure Blob Storage backend, with lazy SDK imports."""

    LOCATOR_PREFIX = "azure://"

    def __init__(
        self,
        *,
        input_container: str,
        output_container: str,
        connection_string: str | None = None,
        account_name: str | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        from azure.storage.blob import BlobServiceClient

        self.input_container = input_container
        self.output_container = output_container
        self._using_credential = False
        self._account_name = account_name
        if connection_string:
            self._service = BlobServiceClient.from_connection_string(connection_string)
            for part in connection_string.split(";"):
                if part.startswith("AccountName="):
                    self._account_name = part.split("=", 1)[1]
        elif account_name:
            from azure.identity import DefaultAzureCredential

            self._service = BlobServiceClient(
                account_url=f"https://{account_name}.blob.core.windows.net",
                credential=DefaultAzureCredential(),
            )
            self._using_credential = True
        else:
            raise ValueError("Azure storage requires a connection string or account name")
        self.cache_dir = Path(cache_dir or tempfile.gettempdir()) / "duke-blob-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _locator(self, container: str, key: str) -> str:
        return f"{self.LOCATOR_PREFIX}{container}/{key.lstrip('/')}"

    def _blob(self, locator: str):
        if locator.startswith(self.LOCATOR_PREFIX):
            path = locator[len(self.LOCATOR_PREFIX):]
            container, separator, key = path.partition("/")
            if not separator or not container or not key:
                raise ValueError(f"invalid blob locator: {locator!r}")
        elif is_blob_uri(locator):
            parsed = urlparse(locator)
            container, separator, key = parsed.path.lstrip("/").partition("/")
            if not separator:
                raise ValueError(f"invalid blob locator: {locator!r}")
        else:
            raise ValueError(f"not a blob locator: {locator!r}")
        return container, key, self._service.get_blob_client(container, key)

    def save_upload(
        self, src: BinaryIO, *, key: str, content_type: str | None = None
    ) -> str:
        from azure.storage.blob import ContentSettings

        client = self._service.get_blob_client(self.input_container, key)
        settings = ContentSettings(content_type=content_type) if content_type else None
        client.upload_blob(src, overwrite=True, content_settings=settings)
        return self._locator(self.input_container, key)

    def open_read(self, locator: str) -> BinaryIO:
        return self.ensure_local(locator).open("rb")

    def ensure_local(self, locator: str, *, filename_hint: str = "") -> Path:
        if is_fm_url(locator):
            return fm_download_to(locator, self.cache_dir, filename_hint=filename_hint)
        if not is_blob_uri(locator):
            return Path(locator)
        container, key, client = self._blob(locator)
        target = (self.cache_dir / container / key).resolve()
        target.relative_to(self.cache_dir.resolve())
        if target.exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as handle:
            client.download_blob().readinto(handle)
        return target

    def publish_output(self, local_path: Path | str, *, key: str) -> str:
        with Path(local_path).open("rb") as handle:
            client = self._service.get_blob_client(self.output_container, key)
            client.upload_blob(handle, overwrite=True)
        return self._locator(self.output_container, key)

    def exists(self, locator: str) -> bool:
        if is_fm_url(locator):
            return fm_url_exists(locator)
        return self._blob(locator)[2].exists()

    def delete(self, locator: str) -> None:
        self._blob(locator)[2].delete_blob()

    def signed_url(self, locator: str, ttl_seconds: int = 900) -> str:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        container, key, client = self._blob(locator)
        if self._using_credential:
            now = datetime.now(timezone.utc)
            delegation = self._service.get_user_delegation_key(
                now - timedelta(minutes=5), now + timedelta(seconds=ttl_seconds)
            )
            sas = generate_blob_sas(
                account_name=self._account_name,
                container_name=container,
                blob_name=key,
                user_delegation_key=delegation,
                permission=BlobSasPermissions(read=True),
                expiry=now + timedelta(seconds=ttl_seconds),
            )
        else:
            raise RuntimeError("A storage account key is required for signed URLs")
        return f"{client.url}?{sas}"

    def size(self, locator: str) -> int:
        return self._blob(locator)[2].get_blob_properties().size


_backend: StorageBackend | None = None
_backend_lock = threading.Lock()


def get_storage() -> StorageBackend:
    """Return a process-wide storage backend selected by environment variables."""
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                mode = os.getenv("STORAGE_BACKEND", "local").lower()
                if mode == "azure_blob":
                    _backend = AzureBlobBackend(
                        input_container=os.getenv("AZURE_STORAGE_INPUT_CONTAINER", "inputs"),
                        output_container=os.getenv("AZURE_STORAGE_OUTPUT_CONTAINER", "outputs"),
                        connection_string=os.getenv("AZURE_STORAGE_CONNECTION_STRING") or None,
                        account_name=os.getenv("AZURE_STORAGE_ACCOUNT_NAME") or None,
                    )
                elif mode == "local":
                    _backend = LocalFsBackend(
                        Path(os.getenv("UPLOAD_DIR", "uploads")),
                        Path(os.getenv("APP_OUTPUT_DIR", "outputs")),
                    )
                else:
                    raise ValueError(f"unknown STORAGE_BACKEND: {mode}")
    return _backend
