"""
TDD tests for auto-reply feature.

Tests that when a question matches a cluster with a drafted FAQ,
the /check response includes faq_url, faq_answer, and similarity_score
so n8n can auto-reply in Slack.
"""
import os
import json
import tempfile

import pytest
from fastapi.testclient import TestClient

# Use a temp DB for each test so we don't touch real data
os.environ["DB_PATH"] = ""  # Will be set per-test in fixture
os.environ["OPENAI_API_KEY"] = "test-key"

# We need to mock the embedding function since we don't want real API calls.
# We'll patch get_embedding to return deterministic vectors.
import main


def _make_embedding(seed: float) -> list[float]:
    """Create a deterministic 1536-dim unit vector from a seed.
    Vectors with close seeds will have high cosine similarity."""
    import numpy as np
    rng = np.random.RandomState(int(seed * 1000))
    vec = rng.randn(1536)
    vec = vec / np.linalg.norm(vec)
    return vec.tolist()


# Pre-compute embeddings that are very similar (same seed) and dissimilar (different seed)
EMBED_PASSWORD_1 = _make_embedding(1.0)
EMBED_PASSWORD_2 = _make_embedding(1.0)  # Identical = similarity 1.0
EMBED_UNRELATED = _make_embedding(99.0)  # Different seed = low similarity


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Give each test a fresh SQLite database."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    main.DB_PATH = db_path
    yield db_path


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def mock_embeddings(monkeypatch):
    """Mock get_embedding to return deterministic vectors based on text content."""
    call_count = {"n": 0}

    def _fake_embedding(text: str) -> list[float]:
        # Return similar embeddings for password-related questions,
        # different embedding for unrelated questions
        if "password" in text.lower() or "reset" in text.lower():
            return EMBED_PASSWORD_1
        return EMBED_UNRELATED

    monkeypatch.setattr(main, "get_embedding", _fake_embedding)


# ─── helpers ───

def _seed_cluster_with_faq(client, mock_embeddings):
    """Seed a cluster with 3 password questions and mark it as drafted with URL + answer."""
    # Add 3 questions to form a cluster and hit the threshold
    client.post("/check", json={"text": "How do I reset my password?"})
    client.post("/check", json={"text": "Where do I reset my password?"})
    resp = client.post("/check", json={"text": "I need to reset my password"})
    data = resp.json()
    cluster_id = data["cluster_id"]
    assert data["cluster_count"] >= 3

    # Mark as drafted with URL and answer
    client.post(f"/clusters/{cluster_id}/mark-drafted", json={
        "notion_url": "https://notion.so/faq-reset-password-abc123",
        "answer": "Go to Settings > Security > Reset Password."
    })
    return cluster_id


# ─── 1. Schema tests ───

class TestSchema:
    def test_clusters_table_has_faq_url_column(self, fresh_db):
        import sqlite3
        conn = main.get_db()
        cursor = conn.execute("PRAGMA table_info(clusters)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert "faq_url" in columns

    def test_clusters_table_has_faq_answer_column(self, fresh_db):
        import sqlite3
        conn = main.get_db()
        cursor = conn.execute("PRAGMA table_info(clusters)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert "faq_answer" in columns


# ─── 2. mark-drafted accepts and stores URL + answer ───

class TestMarkDrafted:
    def test_mark_drafted_accepts_notion_url_and_answer(self, client, mock_embeddings):
        # Create a cluster first
        client.post("/check", json={"text": "How do I reset my password?"})
        resp = client.post("/check", json={"text": "Where do I reset my password?"})
        cluster_id = resp.json()["cluster_id"]

        # Mark drafted with URL and answer
        resp = client.post(f"/clusters/{cluster_id}/mark-drafted", json={
            "notion_url": "https://notion.so/faq-abc123",
            "answer": "Go to Settings > Security."
        })
        assert resp.status_code == 200

        # Verify stored in DB
        import sqlite3
        conn = main.get_db()
        row = conn.execute(
            "SELECT faq_url, faq_answer FROM clusters WHERE id = ?", (cluster_id,)
        ).fetchone()
        conn.close()
        assert row[0] == "https://notion.so/faq-abc123"
        assert row[1] == "Go to Settings > Security."

    def test_mark_drafted_without_body_still_works(self, client, mock_embeddings):
        """Backward compatibility: calling mark-drafted with no body still sets faq_drafted=1."""
        client.post("/check", json={"text": "How do I reset my password?"})
        resp = client.post("/check", json={"text": "Where do I reset my password?"})
        cluster_id = resp.json()["cluster_id"]

        resp = client.post(f"/clusters/{cluster_id}/mark-drafted")
        assert resp.status_code == 200

        import sqlite3
        conn = main.get_db()
        row = conn.execute(
            "SELECT faq_drafted, faq_url, faq_answer FROM clusters WHERE id = ?",
            (cluster_id,)
        ).fetchone()
        conn.close()
        assert row[0] == 1  # faq_drafted still set
        assert row[1] is None  # no URL
        assert row[2] is None  # no answer


# ─── 3. /check response includes auto-reply fields ───

class TestCheckAutoReply:
    def test_check_returns_similarity_score(self, client, mock_embeddings):
        """Every matched response should include similarity_score."""
        client.post("/check", json={"text": "How do I reset my password?"})
        resp = client.post("/check", json={"text": "Where do I reset my password?"})
        data = resp.json()
        assert "similarity_score" in data
        assert isinstance(data["similarity_score"], float)
        assert data["similarity_score"] >= 0.70

    def test_check_returns_faq_url_and_answer_when_drafted(self, client, mock_embeddings):
        """When question matches a cluster with a drafted FAQ, return the URL and answer."""
        cluster_id = _seed_cluster_with_faq(client, mock_embeddings)

        # Now ask the same question again
        resp = client.post("/check", json={"text": "How do I reset my password?"})
        data = resp.json()

        assert data["faq_drafted"] is True
        assert data["faq_url"] == "https://notion.so/faq-reset-password-abc123"
        assert data["faq_answer"] == "Go to Settings > Security > Reset Password."

    def test_check_returns_null_faq_fields_when_not_drafted(self, client, mock_embeddings):
        """When cluster exists but no FAQ drafted, faq_url and faq_answer are null."""
        client.post("/check", json={"text": "How do I reset my password?"})
        resp = client.post("/check", json={"text": "Where do I reset my password?"})
        data = resp.json()

        assert data["faq_drafted"] is False
        assert data.get("faq_url") is None
        assert data.get("faq_answer") is None

    def test_check_returns_zero_similarity_for_new_question(self, client, mock_embeddings):
        """Brand new question with no match returns similarity_score 0.0."""
        resp = client.post("/check", json={"text": "How do I reset my password?"})
        data = resp.json()

        assert data["status"] == "new"
        assert data.get("similarity_score", 0.0) == 0.0

    def test_check_returns_similarity_for_cluster_creation(self, client, mock_embeddings):
        """When two questions form a new cluster, similarity_score is returned."""
        client.post("/check", json={"text": "How do I reset my password?"})
        resp = client.post("/check", json={"text": "Where do I reset my password?"})
        data = resp.json()

        assert data["status"] == "matched"
        assert "similarity_score" in data
        assert data["similarity_score"] >= 0.70
