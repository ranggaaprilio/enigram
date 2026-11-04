"""Against the real Jev API (about $0.0002 per run): the s05 register case of
eval/annotation-spike.yaml. Needs TYPESAFE_API_KEY and the pinned checkout that `enigram golden
check` makes under data/eval/repos."""

import pytest

from enigram.adapters.driven.jev_decider import JevDecider
from enigram.application.decisions import question
from enigram.config import Config

pytestmark = pytest.mark.live_jev

REPO_DIR = "ranggaaprilio-nestjs-boilerplate@70ae60dbb609"
REGISTER = ("src/auth/auth.service.ts", 201, 233)
REGISTER_ID = "src/auth/auth.service.ts#AuthService.register"
TRUE_CLAIM = "Registering creates the user and sends a confirmation email."
FALSE_CLAIM = "Registration requires an invite code issued by an admin."
# Measured 2026-11-03 (jev-1.14.0): a claim about an absence ("Registration needs no invite
# code.") came back says_nothing (literal reading), and the absence question below scored 0.22
# on the register body and 0.43 on the whole file. With the 2026-11-04 wording of `answerable`
# ("including an answer that something is absent") it scores 0.32 and 0.54.
ABSENCE_QUESTION = "Does registering a new account need an invite code?"


def _source() -> list[str]:
    cfg = Config.from_env()
    path = cfg.eval_dir / "repos" / REPO_DIR / REGISTER[0]
    if not path.is_file():
        pytest.skip(f"{path} missing; run `enigram golden check eval/annotation-spike.yaml`")
    return path.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def register_body() -> str:
    lines = _source()
    return "\n".join(f"{n}: {lines[n - 1]}" for n in range(REGISTER[1], REGISTER[2] + 1))


@pytest.fixture
def whole_file() -> str:
    return "\n".join(_source())


@pytest.fixture
def jev() -> JevDecider:
    cfg = Config.from_env()
    if not cfg.jev_api_key:
        pytest.skip("TYPESAFE_API_KEY not set")
    return JevDecider(cfg)


async def test_claim_relation_on_the_register_body(jev, register_body):
    state = {"context": [register_body], "claims": {"1": TRUE_CLAIM, "2": FALSE_CLAIM}}
    questions = {
        "1": question("claim_relation", claim="claims.1"),
        "2": question("claim_relation", claim="claims.2"),
    }

    decisions = await jev.decide(state, questions)

    assert decisions.provider == "jev" and decisions.model.startswith("jev-")
    assert decisions.tokens_in > 0 and decisions.secs < 5
    true, false = decisions.answers["1"], decisions.answers["2"]
    assert true.value == "supports" and true.probabilities["supports"] >= 0.8
    assert false.value == "contradicts"


async def test_note_conflict_on_the_s05_note(jev, register_body):
    state = {"note": FALSE_CLAIM, "code": {REGISTER_ID: register_body}}

    decisions = await jev.decide(state, {"conflict": question("note_conflict")})

    assert decisions.answers["conflict"].value >= 0.8


async def test_answerable_on_the_whole_file(jev, whole_file):
    answerable = question("answerable")

    async def p(asked: str) -> float:
        d = await jev.decide({"question": asked, "evidence": whole_file}, {"a": answerable})
        assert d.tokens_in > 0 and d.secs < 5
        return float(d.answers["a"].value)

    assert await p("What does registering a new account do?") >= 0.8
    assert await p("What is the rate limit of the login endpoint?") <= answerable.threshold
    # An absence question: "no" is the answer, so the guard must not abstain on it.
    assert await p(ABSENCE_QUESTION) > answerable.threshold


async def test_answerable_on_the_register_body(jev, register_body):
    answerable = question("answerable")

    async def p(asked: str) -> float:
        d = await jev.decide({"question": asked, "evidence": register_body}, {"a": answerable})
        return float(d.answers["a"].value)

    # The body is where an invite-code check would have to be; the port lives elsewhere.
    assert await p(ABSENCE_QUESTION) > answerable.threshold
    assert await p("What port does the API listen on by default?") <= answerable.threshold
