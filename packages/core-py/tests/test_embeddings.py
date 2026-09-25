from __future__ import annotations

import hashlib
import math
import subprocess
import sys

import pytest

from agenttwin_core.embeddings import (
    HASHING_DIMS,
    HASHING_MODEL,
    MAX_TEXT_CHARS,
    HashingEmbedder,
    cosine,
    features,
    is_zero,
    vector_literal,
)

E = HashingEmbedder()
SAMPLE = "Refund the payment of order ORD-1001 above the refund_payment limit."


def words(text: str) -> set[str]:
    return {f[2:] for f in features(text) if f.startswith("w:")}


def test_the_hashing_model_is_pinned() -> None:
    # Stored vectors are compared with new ones of the same model: a change
    # to the features, the stemming or the hashing that keeps the model name
    # would compare vectors of two different spaces without any error.
    # Changing this checksum means changing HASHING_MODEL too.
    assert (HASHING_MODEL, E.dims) == ("hashing-v1", HASHING_DIMS) == ("hashing-v1", 256)
    digest = hashlib.sha256(vector_literal(E.embed_one(SAMPLE)).encode()).hexdigest()
    assert digest == "8832e2cab6a4982d2a306360d7bc290ec875d30aa05917fdea67e66462f02bb3"


def test_vectors_do_not_depend_on_the_process() -> None:
    # Python's hash() of a string changes between processes; the embedding must not.
    code = (
        "from agenttwin_core.embeddings import HashingEmbedder, vector_literal;"
        f"print(vector_literal(HashingEmbedder().embed_one({SAMPLE!r})))"
    )
    runs = {
        subprocess.run(  # noqa: S603 - this interpreter, fixed code
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": seed, "PATH": ""},
        ).stdout.strip()
        for seed in ("1", "2")
    }
    assert runs == {vector_literal(E.embed_one(SAMPLE))}


def test_words_are_stemmed_so_forms_of_a_word_agree() -> None:
    assert words("refund refunds refunded refunding") == {"refund"}
    assert words("policy policies") == {"policy"}
    assert words("retry retries retrying") == {"retry"}
    assert words("issue issued approve approved") == {"issu", "approv"}
    assert words("process processes box boxes match matches") == {"process", "box", "match"}
    # Words that only look plural keep their end.
    assert words("status address analysis") == {"status", "address", "analysis"}


def test_identifiers_count_as_a_whole_and_as_their_words() -> None:
    f = features("call refund_payment and lookupOrder on tool:get_refund_policy")
    assert {"c:refund_payment", "c:lookup_order", "c:get_refund_policy"} <= set(f)
    assert {"w:refund", "w:payment", "w:lookup", "w:order", "w:get", "w:policy", "w:tool", "w:call"} <= set(f)
    assert f["w:refund"] == 2
    # Hyphenated names too: "cross-tenant" is one identifier and two words.
    assert {"c:cross_tenant", "w:cross", "w:tenant"} <= set(features("a cross-tenant order"))


def test_stopwords_and_single_characters_carry_nothing() -> None:
    assert features("the and of a to is it I") == {}
    assert is_zero(E.embed_one("The and of a to is it"))
    assert is_zero(E.embed_one(""))
    # A joined identifier made only of stopwords is dropped with its words.
    assert features("to-do") == {}


def test_neighbouring_words_make_pairs() -> None:
    f = features("refund over the limit")
    assert f["b:refund over"] == 0  # "over" is a stopword
    assert f["b:refund limit"] == 1


def test_text_beyond_the_limit_is_not_read() -> None:
    text = "x" * MAX_TEXT_CHARS + " refund"
    assert "w:refund" not in features(text)
    assert "w:refund" in features(text[-50:])


def test_vectors_are_unit_length() -> None:
    for text in (SAMPLE, "refund", "a much longer text about refunds, orders and the refund policy " * 20):
        v = E.embed_one(text)
        assert len(v) == HASHING_DIMS
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9)


def test_related_texts_are_closer_than_unrelated_ones() -> None:
    scenario = E.embed_one(
        "refund over limit. The customer asks for a 450 USD refund while the automatic refund "
        "limit is 100 USD; the agent hands the request to a human specialist. refund_payment "
        "escalate_to_human refund-limit"
    )
    related = E.embed_one("Refunds above the 100 USD limit are escalated to a human. mentions refund_payment")
    unrelated = E.embed_one("Export all personal data of a customer account (administrators only)")
    near, far = cosine(scenario, related), cosine(scenario, unrelated)
    assert near > 0.3 > far + 0.15
    assert math.isclose(cosine(scenario, scenario), 1.0, rel_tol=1e-9)


@pytest.mark.anyio
async def test_embed_is_the_batch_of_embed_one() -> None:
    texts = [SAMPLE, "", "refund policy"]
    assert await E.embed(texts) == [E.embed_one(t) for t in texts]


def test_cosine_and_literals() -> None:
    assert cosine([1.0, 0.0], [0.0, 0.0]) == 0.0
    assert math.isclose(cosine([1.0, 1.0], [2.0, 2.0]), 1.0)
    with pytest.raises(ValueError, match="dimensions"):
        cosine([1.0], [1.0, 0.0])
    assert vector_literal([0.5, -0.25, 0.0, 1e-9]) == "[0.5,-0.25,0,1e-09]"


def test_the_dimension_is_bounded() -> None:
    with pytest.raises(ValueError):
        HashingEmbedder(dims=8)
    assert len(HashingEmbedder(dims=64).embed_one(SAMPLE)) == 64
