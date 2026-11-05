"""One text document saved either as a local file or as an S3 object (`s3://bucket/key`),
so a container keeps what users saved across deploys. Used for saved data points and
saved scenarios."""

from __future__ import annotations

from pathlib import Path


class Document:
    def __init__(self, location: str | Path, s3=None):
        self.location = str(location)
        self._s3 = None
        if self.location.startswith("s3://"):
            self._bucket, _, self._key = self.location[len("s3://"):].partition("/")
            if not self._bucket or not self._key:
                raise ValueError(f"bad S3 location {self.location!r}: expected s3://bucket/key")
            if s3 is None:
                import boto3  # only needed for s3:// locations: install with the `aws` extra
                s3 = boto3.client("s3")
            self._s3 = s3

    def read(self) -> str | None:
        """The text, or None when nothing is saved yet."""
        if self._s3 is None:
            path = Path(self.location)
            return path.read_text() if path.exists() else None
        try:
            return self._s3.get_object(Bucket=self._bucket, Key=self._key)["Body"].read().decode()
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise

    def write(self, text: str) -> None:
        if self._s3 is not None:
            self._s3.put_object(Bucket=self._bucket, Key=self._key, Body=text.encode(), ContentType="application/yaml")
            return
        path = Path(self.location)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        tmp.replace(path)
