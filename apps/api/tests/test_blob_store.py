"""BlobStore (plans/stateless.md, S2 and S7): one contract on the test folder store and an
S3-compatible bucket (locally:
ENIGRAM_TEST_S3_URL=s3://bucket/prefix with AWS_ENDPOINT_URL and keys)."""

import os
from dataclasses import replace
from uuid import uuid4

import pytest
from local_blob_store import LocalBlobStore, books_dir

from enigram import bootstrap
from enigram.adapters.driven.s3_blob_store import S3BlobStore, parse_blob_url
from enigram.bootstrap import build_deps
from enigram.config import Config
from enigram.domain.errors import EnigramError, NotFound

REAL_NEW_BLOB_STORE = bootstrap._new_blob_store  # before conftest's fake_blob_store swaps it


@pytest.fixture(params=["local", pytest.param("s3", marks=pytest.mark.s3)])
async def blobs(request, cfg):
    if request.param == "local":
        yield LocalBlobStore(books_dir(cfg))
        return
    # Its own workspace, so the run never sees another one's files.
    store = S3BlobStore(os.environ["ENIGRAM_TEST_S3_URL"], f"test_{uuid4().hex[:8]}")
    try:
        yield store
    finally:
        for name in await store.names():
            await store.delete(name)


async def test_round_trip_list_and_delete(blobs, tmp_path):
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4 one")
    assert await blobs.names() == []

    await blobs.put("b.pdf", src)
    await blobs.put("a.md", src)
    src.write_bytes(b"%PDF-1.4 two")
    await blobs.put("b.pdf", src)  # replaces

    assert await blobs.names() == ["a.md", "b.pdf"]
    await blobs.get_to("b.pdf", tmp_path / "out.pdf")
    assert (tmp_path / "out.pdf").read_bytes() == b"%PDF-1.4 two"

    await blobs.delete("a.md")
    await blobs.delete("a.md")  # already gone: no error
    assert await blobs.names() == ["b.pdf"]


async def test_get_of_a_missing_name_is_not_found(blobs, tmp_path):
    with pytest.raises(NotFound, match="ghost.pdf"):
        await blobs.get_to("ghost.pdf", tmp_path / "out.pdf")


async def test_local_store_hides_its_partial_writes(cfg):
    books_dir(cfg).mkdir(parents=True)
    (books_dir(cfg) / ".abc.paper.pdf.part").write_bytes(b"half")
    assert await LocalBlobStore(books_dir(cfg)).names() == []


def test_s3_keys_are_per_workspace_under_the_prefix():
    assert parse_blob_url("s3://bucket/a/b/") == ("bucket", "a/b")
    assert S3BlobStore("s3://bucket/a/b", "ws", client=object()).prefix == "a/b/ws/books/"
    assert S3BlobStore("s3://bucket", "ws", client=object()).prefix == "ws/books/"


def test_bootstrap_uses_the_bucket(cfg, monkeypatch):
    monkeypatch.setattr(bootstrap, "_new_blob_store", REAL_NEW_BLOB_STORE)
    # boto3 builds its client without a network call; it needs a region.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    s3 = build_deps(replace(cfg, blob_url="s3://bucket/enigram")).new_blob_store()
    assert isinstance(s3, S3BlobStore) and s3.prefix == "enigram/default/books/"


def test_blob_url_is_required(clean_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key-not-real")
    monkeypatch.setenv("VOYAGE_API_KEY", "vo-key-not-real")
    monkeypatch.delenv("BLOB_URL")
    with pytest.raises(EnigramError, match="BLOB_URL is required"):
        Config.from_env(clean_env)


@pytest.mark.parametrize("value", ["bucket/x", "s3://", "https://bucket"])
def test_blob_url_must_be_an_s3_url(clean_env, monkeypatch, value):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key-not-real")
    monkeypatch.setenv("VOYAGE_API_KEY", "vo-key-not-real")
    monkeypatch.setenv("BLOB_URL", value)
    with pytest.raises(EnigramError, match="BLOB_URL must be"):
        Config.from_env(clean_env)
