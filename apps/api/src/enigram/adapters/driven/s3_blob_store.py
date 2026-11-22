"""BlobStore on an S3-compatible bucket (BLOB_URL=s3://bucket[/prefix]), so the API needs no disk
shared with the worker (plans/stateless.md, S2). Keys: <prefix>/<workspace>/books/<name>.

Credentials and region come from the standard AWS environment (AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, AWS_REGION, or an instance role); AWS_ENDPOINT_URL points at MinIO or
another S3-compatible store. boto3 is blocking, so every call runs in a thread."""

import asyncio
from pathlib import Path
from typing import Any

from enigram.domain.errors import NotFound


def parse_blob_url(url: str) -> tuple[str, str]:
    """(bucket, prefix) from s3://bucket[/prefix]; the prefix has no slash at either end."""
    bucket, _, prefix = url.removeprefix("s3://").partition("/")
    return bucket, prefix.strip("/")


class S3BlobStore:
    def __init__(self, url: str, workspace: str, client: Any = None):
        self.bucket, prefix = parse_blob_url(url)
        self.prefix = f"{prefix}/{workspace}/books/" if prefix else f"{workspace}/books/"
        if client is None:
            import boto3  # only deployments with BLOB_URL load it

            client = boto3.client("s3")
        self.client = client

    def _key(self, name: str) -> str:
        return self.prefix + name

    async def names(self) -> list[str]:
        def run() -> list[str]:
            pages = self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self.prefix
            )
            keys = [o["Key"] for page in pages for o in page.get("Contents", [])]
            # Not in a "subfolder": names are flat, like the books folder.
            return sorted(n for k in keys if "/" not in (n := k[len(self.prefix) :]) and n)

        return await asyncio.to_thread(run)

    async def put(self, name: str, src: Path) -> None:
        await asyncio.to_thread(self.client.upload_file, str(src), self.bucket, self._key(name))

    async def get_to(self, name: str, dest: Path) -> None:
        from botocore.exceptions import ClientError

        try:
            await asyncio.to_thread(
                self.client.download_file, self.bucket, self._key(name), str(dest)
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                raise NotFound(
                    f"No source file {name!r} in s3://{self.bucket}/{self.prefix}"
                ) from e
            raise

    async def delete(self, name: str) -> None:
        # S3 answers a delete of a missing key with success.
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=self._key(name))
