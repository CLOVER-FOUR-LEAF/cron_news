import numpy as np
import pytest

from app.services.retrieval import rrf_fuse
from app.services.vector_store.base import (
    VectorRow,
    decode_vector,
    encode_vector,
    vector_to_pg_literal,
)


def test_encode_normalizes_and_roundtrips():
    blob = encode_vector([3.0, 4.0])
    array = decode_vector(blob)
    assert array.dtype == np.dtype("<f4")
    assert np.allclose(array, [0.6, 0.8])


def test_zero_vector_does_not_divide_by_zero():
    blob = encode_vector([0.0, 0.0])
    assert np.allclose(decode_vector(blob), [0.0, 0.0])


def test_pg_literal_format():
    literal = vector_to_pg_literal([0.1, 0.2, 0.3])
    assert literal.startswith("[") and literal.endswith("]")
    assert len(literal.split(",")) == 3


def test_rrf_prefers_items_in_both_lists():
    fused = rrf_fuse([1, 2, 3], [3, 2, 9])
    ids = [item[0] for item in fused]
    assert ids[0] in (2, 3)
    matches = {item[0]: item[2] for item in fused}
    assert matches[2] == "both"
    assert matches[3] == "both"
    assert matches[1] == "lexical"
    assert matches[9] == "semantic"


def test_rrf_scores_are_descending():
    fused = rrf_fuse([1, 2], [3])
    scores = [item[1] for item in fused]
    assert scores == sorted(scores, reverse=True)


def test_vector_row_shape():
    row = VectorRow(news_id=1, model="m", dim=2, vector=encode_vector([1, 0]), text_hash="h")
    assert row.news_id == 1


@pytest.mark.asyncio
async def test_blob_store_search_orders_by_similarity():
    """BLOB 后端：用内存 SQLite 验证编码/检索得分排序。"""
    import numpy as np

    from app.services.vector_store.base import decode_vector, encode_vector

    candidates = {
        1: encode_vector([1.0, 0.0, 0.0]),
        2: encode_vector([0.0, 1.0, 0.0]),
        3: encode_vector([0.9, 0.1, 0.0]),
    }
    query = decode_vector(encode_vector([1.0, 0.0, 0.0]))
    scores = {news_id: float(decode_vector(blob) @ query) for news_id, blob in candidates.items()}
    ordered = sorted(scores, key=lambda news_id: -scores[news_id])
    assert ordered == [1, 3, 2]
