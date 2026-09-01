"""S3-compatible object store for recording segments."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError


class ObjectStore(Protocol):
    def put(self, key: str, path: Path) -> str: ...


class S3ObjectStore:
    def __init__(
        self,
        *,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
    ) -> None:
        self.bucket = bucket
        kwargs: dict[str, object] = {
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": region,
            "config": Config(s3={"addressing_style": "path"}),
        }
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        self.client: BaseClient = boto3.client("s3", **kwargs)

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            create_kwargs: dict[str, object] = {"Bucket": self.bucket}
            if self.client.meta.region_name not in {"us-east-1", None, ""}:
                create_kwargs["CreateBucketConfiguration"] = {
                    "LocationConstraint": self.client.meta.region_name
                }
            try:
                self.client.create_bucket(**create_kwargs)
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code", "")
                if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise

    def put(self, key: str, path: Path) -> str:
        with path.open("rb") as handle:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=handle,
                ContentType="video/mp4",
            )
        return str(response.get("ETag", "")).strip('"')
