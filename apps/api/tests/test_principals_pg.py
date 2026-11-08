import os
from uuid import uuid4

import asyncpg
import pytest

from enigram.adapters.driven.postgres_principals import PostgresPrincipals
from enigram.domain.errors import EnigramError

pytestmark = pytest.mark.postgres


@pytest.fixture
async def pool():
    pool = await asyncpg.create_pool(os.environ["ENIGRAM_TEST_PG_DSN"])
    yield pool
    await pool.close()


@pytest.fixture
async def principals(pool):
    repo = PostgresPrincipals(pool)
    await repo.ensure_schema()
    tag = uuid4().hex[:8]
    yield repo, tag
    await pool.execute("DELETE FROM principals WHERE name LIKE $1", f"%-{tag}")


async def test_keys_roles_owner_and_revocation(principals):
    repo, tag = principals
    name = f"laptop-{tag}"
    created = await repo.create(name, "key", f"hash-{tag}", "default")

    assert (await repo.by_key_hash(f"hash-{tag}")) == created
    with pytest.raises(EnigramError, match="already exists"):
        await repo.create(name, "key", f"other-{tag}", "default")
    await repo.set_role("default", created.id, "viewer")
    await repo.set_role("default", created.id, "contributor")  # replaces
    await repo.set_role("ws-a", created.id, "admin")
    assert await repo.roles(created.id) == {"default": "contributor", "ws-a": "admin"}
    assert await repo.remove_member("ws-a", created.id) is True
    assert await repo.remove_member("ws-a", created.id) is False
    with pytest.raises(EnigramError, match="role must be"):
        await repo.set_role("default", created.id, "god")

    await repo.set_owner(created.id, True)
    await repo.set_key_hash(created.id, f"new-{tag}")
    assert (await repo.by_key_hash(f"new-{tag}")).owner is True
    assert await repo.by_key_hash(f"hash-{tag}") is None
    await repo.revoke(created.id)
    assert await repo.by_key_hash(f"new-{tag}") is None
    (row,) = [r for r in await repo.list() if r[0].name == name]
    assert (row[1], row[2]) == (True, {"default": "contributor"})
