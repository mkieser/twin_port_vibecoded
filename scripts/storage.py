"""
Where the live data files live: each folder is local or an S3 prefix.

  AIS_DIR       AIS positions and analysis    DATA_DIR/ais
  TIDE_DIR      tide levels                   DATA_DIR/tide
  FLOODING_DIR  coastal flooding risk index   DATA_DIR/coastal_flooding

Each is the environment variable of that name if set, e.g. s3://kieser-public/ais,
else the folder in DATA_DIR. Local paths are pathlib.Path, S3 ones plain s3:// strings.

DuckDB reads and writes both kinds; S3 access uses the AWS default credential
chain (environment, ~/.aws, ECS task role, Lambda role), refreshed as it expires.
Listing, existence checks and small text files use boto3 for S3, which is only
imported then.
AWS_ENDPOINT_URL points both at an S3-compatible server instead (MinIO, moto).
DUCKDB_EXTENSION_DIRECTORY, if set, is where DuckDB finds its extensions; the
deploy image installs them there at build time, as Lambda can't write them later.

Needs duckdb, plus boto3 for S3 (pip install duckdb boto3).
"""

import os
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from load_rasters import DATA_DIR


def is_s3(path):
    return str(path).startswith("s3://")


def location(text):
    """A path from a string: kept as text for s3://, a Path otherwise."""
    return text.rstrip("/") if is_s3(text) else Path(text)


AIS_DIR = location(os.environ.get("AIS_DIR") or str(DATA_DIR / "ais"))
TIDE_DIR = location(os.environ.get("TIDE_DIR") or str(DATA_DIR / "tide"))
FLOODING_DIR = location(os.environ.get("FLOODING_DIR") or str(DATA_DIR / "coastal_flooding"))


def join(base, *parts):
    return "/".join([base.rstrip("/"), *parts]) if is_s3(base) else Path(base, *parts)


def sibling(path, name):
    """`name` in the same folder as `path`."""
    return join(str(path).rsplit("/", 1)[0], name) if is_s3(path) else Path(path).with_name(name)


def basename(path):
    return str(path).rsplit("/", 1)[-1]


@cache
def _s3_client():
    import boto3
    return boto3.client("s3")


@cache
def _bucket_region(bucket):
    return _s3_client().head_bucket(Bucket=bucket)["BucketRegion"]


def _bucket_key(path):
    bucket, _, key = str(path).removeprefix("s3://").partition("/")
    return bucket, key


def _list(prefix, delimiter=None):
    """S3 objects (or with a delimiter, common prefixes) under `prefix`."""
    bucket, key = _bucket_key(prefix)
    kwargs = {"Bucket": bucket, "Prefix": key.rstrip("/") + "/"}
    if delimiter:
        kwargs["Delimiter"] = delimiter
    for page in _s3_client().get_paginator("list_objects_v2").paginate(**kwargs):
        if delimiter:
            yield from (f"s3://{bucket}/{p['Prefix'].rstrip('/')}" for p in page.get("CommonPrefixes", []))
        else:
            yield from page.get("Contents", [])


def exists(path):
    if not is_s3(path):
        return Path(path).exists()
    from botocore.exceptions import ClientError
    bucket, key = _bucket_key(path)
    try:
        _s3_client().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def read_text(path):
    if not is_s3(path):
        return Path(path).read_text()
    bucket, key = _bucket_key(path)
    return _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read().decode()


def write_text(path, text):
    """Write a small text file whole: locally via a temporary file and a rename."""
    if is_s3(path):
        bucket, key = _bucket_key(path)
        _s3_client().put_object(Bucket=bucket, Key=key, Body=text.encode())
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def subdirs(folder, prefix=""):
    """Folders directly in `folder` whose name starts with `prefix`."""
    if is_s3(folder):
        return [p for p in _list(folder, "/") if basename(p).startswith(prefix)]
    return [p for p in Path(folder).glob(f"{prefix}*") if p.is_dir()]


def list_files(folder, suffix=""):
    """(mtime, path) of the files directly in `folder` ending in `suffix`."""
    if is_s3(folder):
        bucket, key = _bucket_key(folder)
        depth = key.rstrip("/").count("/") + 1   # slashes in a direct child's key
        return [(obj["LastModified"].timestamp(), f"s3://{bucket}/{obj['Key']}")
                for obj in _list(folder)
                if obj["Key"].endswith(suffix) and obj["Key"].count("/") == depth]
    return [(f.stat().st_mtime, f) for f in Path(folder).glob(f"*{suffix}") if f.is_file()]


def connect(*paths, spatial=True):
    """In-memory DuckDB in UTC, with spatial loaded unless told not to, able to reach
    S3 if any of `paths` is there."""
    con = duckdb.connect()
    if extensions := os.environ.get("DUCKDB_EXTENSION_DIRECTORY"):
        con.execute("SET extension_directory = ?", [extensions])
    con.execute("SET TimeZone = 'UTC'")
    if spatial:
        con.execute("INSTALL spatial; LOAD spatial")
    buckets = {_bucket_key(p)[0] for p in paths if is_s3(p)}
    if buckets:
        con.execute("INSTALL httpfs; LOAD httpfs; INSTALL aws; LOAD aws")
    for bucket in buckets:   # one secret per bucket, in the bucket's own region
        options = ["TYPE s3", "PROVIDER credential_chain", "REFRESH auto",
                   f"REGION '{_bucket_region(bucket)}'", f"SCOPE 's3://{bucket}'"]
        if endpoint := os.environ.get("AWS_ENDPOINT_URL"):
            url = urlsplit(endpoint)
            options += [f"ENDPOINT '{url.netloc}'", "URL_STYLE 'path'",
                        f"USE_SSL {str(url.scheme == 'https').lower()}"]
        con.execute(f"CREATE SECRET ({', '.join(options)})")
    return con


def copy_to(con, query, dst, options="FORMAT parquet"):
    """COPY (query) TO dst and return the row count. Readers never see a half-written
    file: a local one is written beside dst and renamed, and S3 objects appear whole."""
    if is_s3(dst):
        return con.execute(f"COPY ({query}) TO '{dst}' ({options})").fetchone()[0]
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    n_rows = con.execute(f"COPY ({query}) TO '{tmp}' ({options})").fetchone()[0]
    tmp.replace(dst)
    return n_rows
