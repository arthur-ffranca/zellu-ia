"""MinIO service for document storage and retrieval."""

from typing import Optional, Dict, Any
from pathlib import Path
import mimetypes
from datetime import timedelta
import urllib3
from minio import Minio
from minio.error import S3Error

from config import Settings


class MinioService:
    """Service for uploading and managing documents in MinIO."""

    def __init__(self, settings: Settings):
        """Initialize MinIO service.

        Args:
            settings: Application settings
        """
        self.settings = settings
        self.bucket_name = settings.MINIO_BUCKET
        self.client: Optional[Minio] = None
        self.enabled = False

        # MinIO nao e usado em producao. Com MINIO_ENABLED=false (padrao) nao ha
        # nenhuma chamada de rede no boot: antes, o bucket_exists() contra um
        # endpoint fora do ar (503) segurava o startup por ~2min45s com os retries
        # padrao do urllib3 e derrubava o healthcheck do Coolify.
        if not getattr(settings, "MINIO_ENABLED", False):
            print("[MINIO] Disabled (MINIO_ENABLED=false) - skipping initialization")
            return

        # Initialize MinIO client if configured
        if self._is_configured():
            try:
                self.client = Minio(
                    endpoint=settings.MINIO_ENDPOINT,
                    access_key=settings.MINIO_ACCESS_KEY,
                    secret_key=settings.MINIO_SECRET_KEY,
                    secure=settings.MINIO_USE_SSL,
                    http_client=self._build_http_client(settings),
                )

                # Create bucket if it doesn't exist
                self._ensure_bucket_exists()
                self.enabled = True

                print(f"[MINIO] Service initialized - Endpoint: {settings.MINIO_ENDPOINT}, Bucket: {self.bucket_name}")

            except Exception as e:
                print(f"[MINIO] Failed to initialize: {str(e)}")
                print("[MINIO] MinIO features will be disabled")
                self.enabled = False
        else:
            print("[MINIO] Not configured - MinIO features will be disabled")
            print("[MINIO] To enable, set MINIO_ENDPOINT, MINIO_ACCESS_KEY, and MINIO_SECRET_KEY in .env")

    @staticmethod
    def _build_http_client(settings: Settings) -> urllib3.PoolManager:
        """Pool HTTP com timeout curto e no maximo 1 retry (nunca trava o boot)."""
        timeout_s = float(getattr(settings, "MINIO_TIMEOUT_SECONDS", 5.0) or 5.0)
        return urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=timeout_s, read=timeout_s),
            maxsize=10,
            retries=urllib3.Retry(
                total=1,
                backoff_factor=0.2,
                status_forcelist=[500, 502, 503, 504],
                raise_on_status=False,
            ),
        )

    def _is_configured(self) -> bool:
        """Check if MinIO is properly configured.

        Returns:
            True if all required settings are present
        """
        return bool(
            self.settings.MINIO_ENDPOINT and
            self.settings.MINIO_ACCESS_KEY and
            self.settings.MINIO_SECRET_KEY
        )

    def _ensure_bucket_exists(self):
        """Create bucket if it doesn't exist."""
        try:
            if not self.client.bucket_exists(self.bucket_name):
                self.client.make_bucket(self.bucket_name)
                print(f"[MINIO] Created bucket: {self.bucket_name}")
            else:
                print(f"[MINIO] Bucket exists: {self.bucket_name}")
        except S3Error as e:
            print(f"[MINIO] Error checking/creating bucket: {str(e)}")
            raise

    async def upload_file(
        self,
        file_path: str,
        object_name: Optional[str] = None,
        folder: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Upload a file to MinIO.

        Args:
            file_path: Local path to the file to upload
            object_name: Name to give the file in MinIO (defaults to filename)
            folder: Optional folder/prefix in bucket (e.g., "documents/chat123")

        Returns:
            Dictionary with file info and URL, or None if upload failed
        """
        if not self.enabled:
            print("[MINIO] Upload skipped - MinIO not enabled")
            return None

        try:
            path = Path(file_path)

            # Validate file exists
            if not path.exists():
                print(f"[MINIO] File not found: {file_path}")
                return None

            # Determine object name
            if object_name is None:
                object_name = path.name

            # Add folder prefix if provided
            if folder:
                object_name = f"{folder.rstrip('/')}/{object_name}"

            # Get MIME type
            content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"

            # Upload file
            self.client.fput_object(
                bucket_name=self.bucket_name,
                object_name=object_name,
                file_path=str(path),
                content_type=content_type
            )

            # Get file size
            file_size = path.stat().st_size

            # Generate presigned URL (valid for 7 days)
            url = self.client.presigned_get_object(
                bucket_name=self.bucket_name,
                object_name=object_name,
                expires=timedelta(days=7)
            )

            print(f"[MINIO] Uploaded: {object_name} ({file_size} bytes)")

            return {
                "bucket": self.bucket_name,
                "object_name": object_name,
                "url": url,
                "content_type": content_type,
                "size": file_size,
                "filename": path.name
            }

        except S3Error as e:
            print(f"[MINIO] S3 Error uploading {file_path}: {str(e)}")
            return None
        except Exception as e:
            print(f"[MINIO] Error uploading {file_path}: {str(e)}")
            return None

    async def upload_multiple(
        self,
        file_paths: list[str],
        folder: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Upload multiple files to MinIO.

        Args:
            file_paths: List of local file paths to upload
            folder: Optional folder/prefix in bucket

        Returns:
            Dictionary with upload results
        """
        if not self.enabled:
            return {
                "success": False,
                "error": "MinIO not enabled",
                "uploaded_files": []
            }

        uploaded_files = []
        errors = []

        for file_path in file_paths:
            result = await self.upload_file(file_path, folder=folder)

            if result:
                uploaded_files.append(result)
            else:
                errors.append(f"Failed to upload: {file_path}")

        return {
            "success": len(uploaded_files) > 0,
            "uploaded_files": uploaded_files,
            "total_uploaded": len(uploaded_files),
            "total_failed": len(errors),
            "errors": errors if errors else None
        }

    def get_file_url(
        self,
        object_name: str,
        expires_days: int = 7
    ) -> Optional[str]:
        """
        Get presigned URL for a file in MinIO.

        Args:
            object_name: Name of the object in MinIO
            expires_days: Number of days URL should be valid

        Returns:
            Presigned URL or None if failed
        """
        if not self.enabled:
            return None

        try:
            url = self.client.presigned_get_object(
                bucket_name=self.bucket_name,
                object_name=object_name,
                expires=timedelta(days=expires_days)
            )
            return url
        except S3Error as e:
            print(f"[MINIO] Error getting URL for {object_name}: {str(e)}")
            return None

    def delete_file(self, object_name: str) -> bool:
        """
        Delete a file from MinIO.

        Args:
            object_name: Name of the object to delete

        Returns:
            True if deleted successfully, False otherwise
        """
        if not self.enabled:
            return False

        try:
            self.client.remove_object(
                bucket_name=self.bucket_name,
                object_name=object_name
            )
            print(f"[MINIO] Deleted: {object_name}")
            return True
        except S3Error as e:
            print(f"[MINIO] Error deleting {object_name}: {str(e)}")
            return False

    def list_files(self, prefix: Optional[str] = None) -> list[Dict[str, Any]]:
        """
        List files in MinIO bucket.

        Args:
            prefix: Optional prefix to filter files (e.g., "documents/chat123/")

        Returns:
            List of file information dictionaries
        """
        if not self.enabled:
            return []

        try:
            objects = self.client.list_objects(
                bucket_name=self.bucket_name,
                prefix=prefix,
                recursive=True
            )

            files = []
            for obj in objects:
                files.append({
                    "object_name": obj.object_name,
                    "size": obj.size,
                    "last_modified": obj.last_modified,
                    "etag": obj.etag,
                    "content_type": obj.content_type
                })

            return files

        except S3Error as e:
            print(f"[MINIO] Error listing files: {str(e)}")
            return []
