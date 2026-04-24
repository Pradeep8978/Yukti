"""
yukti/artifacts.py

Helpers to package model directories and optionally upload to S3.
Computes SHA256 checksums and writes metadata JSON.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from yukti.config import settings
import hmac


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def package_model_dir(model_dir: str, out_dir: Optional[str] = None) -> tuple[str, str]:
    """Create a zip archive of model_dir. Returns (archive_path, sha256_hex).
    Places archive in out_dir or a temp folder.
    """
    model_dir = str(model_dir)
    name = Path(model_dir).name or f"model_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    if out_dir is None:
        out_dir = tempfile.mkdtemp()
    os.makedirs(out_dir, exist_ok=True)
    archive_base = os.path.join(out_dir, name)
    archive_path = shutil.make_archive(archive_base, 'zip', model_dir)
    sha = _sha256_file(archive_path)
    return archive_path, sha


def _make_s3_client():
    try:
        import boto3
    except Exception:
        return None
    # Rely on boto3 default credential resolution; optionally use env vars in settings
    kwargs = {}
    if getattr(settings, "aws_access_key_id", ""):
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
    if getattr(settings, "aws_secret_access_key", ""):
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    if getattr(settings, "artifact_registry_s3_region", ""):
        kwargs["region_name"] = settings.artifact_registry_s3_region
    return boto3.client("s3", **kwargs)


def upload_to_s3(archive_path: str, sha: str, s3_bucket: str, s3_prefix: str) -> Optional[str]:
    """Upload the archive to S3 and return the s3 key. Returns None if upload not performed.
    """
    client = _make_s3_client()
    if client is None:
        return None
    filename = os.path.basename(archive_path)
    key = f"{s3_prefix.rstrip('/')}/{datetime.utcnow().strftime('%Y%m%d')}/{filename}"
    try:
        client.upload_file(archive_path, s3_bucket, key)
        # Upload metadata as an adjacent .json
        meta = {
            "sha256": sha,
            "filename": filename,
            "uploaded_at": datetime.utcnow().isoformat(),
        }
        meta_key = key + ".metadata.json"
        client.put_object(Bucket=s3_bucket, Key=meta_key, Body=json.dumps(meta))
        return key
    except Exception:
        return None


def save_metadata_local(meta: dict, out_dir: Optional[str] = None) -> str:
    if out_dir is None:
        out_dir = os.path.join("models", "registry")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"meta_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.json")
    with open(path, "w") as f:
        json.dump(meta, f, indent=2)
    return path


def package_and_publish(model_dir: str, out_dir: Optional[str] = None) -> dict:
    """Package a model directory, compute checksum, optionally upload to S3, and write local metadata.
    Returns metadata dict: {archive_path, sha256, s3_key or None, meta_path}
    """
    archive_path, sha = package_model_dir(model_dir, out_dir=out_dir)
    s3_bucket = getattr(settings, "artifact_registry_s3_bucket", "")
    s3_prefix = getattr(settings, "artifact_registry_s3_prefix", "yukti/models")
    s3_key = None
    if s3_bucket:
        s3_key = upload_to_s3(archive_path, sha, s3_bucket, s3_prefix)
    meta = {
        "model_dir": model_dir,
        "archive_path": archive_path,
        "sha256": sha,
        "s3_bucket": s3_bucket or None,
        "s3_key": s3_key,
        "created_at": datetime.utcnow().isoformat(),
    }
    meta_path = save_metadata_local(meta, out_dir=os.path.join("models", "registry"))
    meta["meta_path"] = meta_path
    # Optional HMAC signing for artifact provenance
    signing_key = getattr(settings, "artifact_registry_signing_key", "")
    if signing_key:
        try:
            sig = hmac.new(signing_key.encode("utf-8"), sha.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()
            sig_path = meta_path + ".sig"
            with open(sig_path, "w") as sf:
                sf.write(sig)
            meta["signature"] = sig
            meta["signature_path"] = sig_path
        except Exception:
            pass
    return meta
