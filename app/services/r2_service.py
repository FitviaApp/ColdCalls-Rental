"""
Cloudflare R2 Service for audio file storage
"""
import uuid
from typing import Optional

import boto3
from botocore.config import Config

from app.config import get_settings

settings = get_settings()


class R2Service:
    """Service for interacting with Cloudflare R2 storage"""

    def __init__(self):
        self._client = None

    @property
    def client(self):
        """Lazy initialization of S3 client"""
        if self._client is None and settings.R2_ACCOUNT_ID:
            self._client = boto3.client(
                's3',
                endpoint_url=f'https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
                aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                config=Config(signature_version='s3v4'),
                region_name='auto'
            )
        return self._client

    @property
    def bucket(self) -> str:
        return settings.R2_BUCKET_NAME

    @property
    def public_url(self) -> str:
        return settings.R2_PUBLIC_URL

    def list_audios(self) -> list:
        """List all audio files in the bucket"""
        if not self.client:
            return []

        try:
            response = self.client.list_objects_v2(
                Bucket=self.bucket,
                Prefix='audios/'
            )

            files = []
            for obj in response.get('Contents', []):
                files.append({
                    'key': obj['Key'],
                    'size': obj['Size'],
                    'last_modified': obj['LastModified'],
                    'url': f"{self.public_url}/{obj['Key']}"
                })
            return files
        except Exception as e:
            print(f"Error listing audios: {e}")
            return []

    def upload_audio(self, file_content: bytes, filename: str, content_type: str) -> dict:
        """
        Upload an audio file to R2

        Args:
            file_content: File bytes
            filename: Original filename
            content_type: MIME type

        Returns:
            dict with 'key' and 'url'
        """
        # Generate unique filename to avoid collisions
        extension = filename.rsplit('.', 1)[-1] if '.' in filename else 'mp3'
        unique_filename = f"{uuid.uuid4().hex}.{extension}"
        key = f"audios/{unique_filename}"

        if self.client:
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=file_content,
                    ContentType=content_type
                )
            except Exception as e:
                print(f"Error uploading to R2: {e}")
                # Fall through to return local-like URL for development
        else:
            print("R2 client not configured, using placeholder URL")

        return {
            'key': key,
            'url': f"{self.public_url}/{key}" if self.public_url else f"/static/audios/{unique_filename}"
        }

    def delete_audio(self, key: str) -> bool:
        """
        Delete an audio file from R2

        Args:
            key: The R2 object key

        Returns:
            True if successful
        """
        if not self.client:
            return True  # No-op in development

        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as e:
            print(f"Error deleting from R2: {e}")
            return False

    def get_audio_url(self, key: str) -> str:
        """Get public URL for an audio file"""
        return f"{self.public_url}/{key}" if self.public_url else f"/static/audios/{key}"

    def get_presigned_url(self, key: str, expires_in: int = 3600) -> Optional[str]:
        """
        Generate a presigned URL for private access

        Args:
            key: The R2 object key
            expires_in: Expiration time in seconds

        Returns:
            Presigned URL or None
        """
        if not self.client:
            return None

        try:
            return self.client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.bucket, 'Key': key},
                ExpiresIn=expires_in
            )
        except Exception as e:
            print(f"Error generating presigned URL: {e}")
            return None


# Singleton instance
r2_service = R2Service()
