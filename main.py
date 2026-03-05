import os
import json
import sqlite3
import numpy as np
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
from openai import OpenAI

app = FastAPI()
@app.get("/")
def root():
    return {"status": "running"}
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY")


def verify_admin_key(x_api_key: str = Header()):
    if not ADMIN_API_KEY or x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

# SQLite setup - use Railway volume path if available, else local
DB_PATH = os.environ.get("DB_PATH", "/data/questions.db")

def get_db():
    """Get database connection and ensure tables exist."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            topic TEXT,
            embedding TEXT NOT NULL,
            cluster_id INTEGER,
            source_channel TEXT,
            source_user TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT,
            count INTEGER DEFAULT 1,
            faq_drafted INTEGER DEFAULT 0,
            faq_url TEXT,
            faq_answer TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def get_embedding(text: str) -> list[float]:
    """Generate embedding via OpenAI API."""
    response = client.embeddings.create(
        input=text,
        model="text-embedding-3-small"
    )
    return response.data[0].embedding


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a = np.array(a)
    b = np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


class QuestionInput(BaseModel):
    text: str
    topic: str | None = None
    source_channel: str | None = None
    source_user: str | None = None


class QuestionResponse(BaseModel):
    status: str  # "new" or "matched"
    cluster_id: int
    cluster_count: int
    similar_questions: list[str]
    faq_drafted: bool = False
    faq_url: str | None = None
    faq_answer: str | None = None
    similarity_score: float = 0.0


@app.post("/check", response_model=QuestionResponse)
def check_question(question: QuestionInput):
    """Check if a question matches an existing cluster."""
    conn = get_db()
    
    # Generate embedding for the new question
    new_embedding = get_embedding(question.text)
    
    # Get all existing questions with embeddings
    rows = conn.execute(
        "SELECT id, text, embedding, cluster_id FROM questions"
    ).fetchall()
    
    best_match = None
    best_similarity = 0.0
    threshold = 0.70  # Tuned via /debug endpoint. Paraphrased questions score 0.70-0.75.
    
    for row in rows:
        row_id, row_text, row_embedding_json, row_cluster_id = row
        row_embedding = json.loads(row_embedding_json)
        sim = cosine_similarity(new_embedding, row_embedding)
        
        if sim > best_similarity and sim >= threshold:
            best_similarity = sim
            best_match = {
                "id": row_id,
                "text": row_text,
                "cluster_id": row_cluster_id
            }
    
    if best_match and best_match["cluster_id"]:
        # Matched to existing cluster
        cluster_id = best_match["cluster_id"]
        
        # Insert the new question into the same cluster
        conn.execute(
            """INSERT INTO questions (text, topic, embedding, cluster_id, 
               source_channel, source_user) VALUES (?, ?, ?, ?, ?, ?)""",
            (question.text, question.topic, json.dumps(new_embedding),
             cluster_id, question.source_channel, question.source_user)
        )
        
        # Update cluster count and timestamp
        conn.execute(
            """UPDATE clusters SET count = count + 1, 
               updated_at = ? WHERE id = ?""",
            (datetime.utcnow().isoformat(), cluster_id)
        )
        conn.commit()
        
        # Get cluster info
        cluster = conn.execute(
            "SELECT count, faq_drafted, faq_url, faq_answer FROM clusters WHERE id = ?", (cluster_id,)
        ).fetchone()
        
        similar = conn.execute(
            "SELECT text FROM questions WHERE cluster_id = ?", (cluster_id,)
        ).fetchall()
        
        conn.close()
        return QuestionResponse(
            status="matched",
            cluster_id=cluster_id,
            cluster_count=cluster[0],
            similar_questions=[r[0] for r in similar],
            faq_drafted=bool(cluster[1]),
            faq_url=cluster[2],
            faq_answer=cluster[3],
            similarity_score=best_similarity
        )
    
    elif best_match and not best_match["cluster_id"]:
        # Matched to a question that doesn't have a cluster yet.
        # Create a new cluster for both questions.
        cursor = conn.execute(
            """INSERT INTO clusters (topic, count, created_at, updated_at) 
               VALUES (?, 2, ?, ?)""",
            (question.topic, datetime.utcnow().isoformat(),
             datetime.utcnow().isoformat())
        )
        cluster_id = cursor.lastrowid
        
        # Assign cluster to the matched question
        conn.execute(
            "UPDATE questions SET cluster_id = ? WHERE id = ?",
            (cluster_id, best_match["id"])
        )
        
        # Insert the new question with the cluster
        conn.execute(
            """INSERT INTO questions (text, topic, embedding, cluster_id, 
               source_channel, source_user) VALUES (?, ?, ?, ?, ?, ?)""",
            (question.text, question.topic, json.dumps(new_embedding),
             cluster_id, question.source_channel, question.source_user)
        )
        conn.commit()
        
        similar = [best_match["text"], question.text]
        conn.close()
        return QuestionResponse(
            status="matched",
            cluster_id=cluster_id,
            cluster_count=2,
            similar_questions=similar,
            similarity_score=best_similarity
        )
    
    else:
        # No match. Store as standalone question.
        conn.execute(
            """INSERT INTO questions (text, topic, embedding, cluster_id, 
               source_channel, source_user) VALUES (?, ?, ?, ?, ?, ?)""",
            (question.text, question.topic, json.dumps(new_embedding),
             None, question.source_channel, question.source_user)
        )
        conn.commit()
        conn.close()
        return QuestionResponse(
            status="new",
            cluster_id=0,
            cluster_count=0,
            similar_questions=[]
        )


@app.get("/clusters")
def list_clusters():
    """List all clusters with their questions. Useful for debugging."""
    conn = get_db()
    clusters = conn.execute(
        "SELECT id, topic, count, faq_drafted, created_at FROM clusters ORDER BY count DESC"
    ).fetchall()
    
    result = []
    for c in clusters:
        questions = conn.execute(
            "SELECT text, created_at FROM questions WHERE cluster_id = ?",
            (c[0],)
        ).fetchall()
        result.append({
            "cluster_id": c[0],
            "topic": c[1],
            "count": c[2],
            "faq_drafted": bool(c[3]),
            "created_at": c[4],
            "questions": [{"text": q[0], "created_at": q[1]} for q in questions]
        })
    
    conn.close()
    return result


class MarkDraftedInput(BaseModel):
    notion_url: str | None = None
    answer: str | None = None


@app.post("/clusters/{cluster_id}/mark-drafted")
def mark_drafted(cluster_id: int, body: MarkDraftedInput | None = None):
    """Mark a cluster as having had its FAQ drafted. Optionally store the Notion URL and answer."""
    conn = get_db()
    if body and (body.notion_url or body.answer):
        conn.execute(
            "UPDATE clusters SET faq_drafted = 1, faq_url = ?, faq_answer = ? WHERE id = ?",
            (body.notion_url, body.answer, cluster_id)
        )
    else:
        conn.execute(
            "UPDATE clusters SET faq_drafted = 1 WHERE id = ?", (cluster_id,)
        )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/env-check")
def env_check():
    """Temporary: confirm ADMIN_API_KEY is loaded. Remove after debugging."""
    key = os.environ.get("ADMIN_API_KEY")
    return {
        "admin_key_set": key is not None,
        "admin_key_length": len(key) if key else 0,
        "module_level_key_set": ADMIN_API_KEY is not None,
        "module_level_key_length": len(ADMIN_API_KEY) if ADMIN_API_KEY else 0,
        "keys_match": key == ADMIN_API_KEY
    }


@app.get("/debug-header")
def debug_header(x_api_key: str = Header(default=None)):
    """Temporary: show exactly what value FastAPI receives for X-Api-Key header."""
    return {
        "received": x_api_key,
        "received_length": len(x_api_key) if x_api_key else 0,
        "matches_env": x_api_key == ADMIN_API_KEY
    }
    
@app.post("/reset", dependencies=[Depends(verify_admin_key)])
def reset_db():
    """Resetting the database for testing."""
    conn = get_db()
    conn.execute("DELETE FROM questions")
    conn.execute("DELETE FROM clusters")
    conn.commit()
    conn.close()
    return {"status": "cleared"}


@app.post("/debug", dependencies=[Depends(verify_admin_key)])
def debug_similarity(question: QuestionInput):
    """Debug endpoint: show similarity scores between a question and all stored questions."""
    conn = get_db()
    new_embedding = get_embedding(question.text)
    rows = conn.execute(
        "SELECT id, text, embedding FROM questions"
    ).fetchall()
    
    results = []
    for row in rows:
        row_id, row_text, row_embedding_json = row
        row_embedding = json.loads(row_embedding_json)
        sim = cosine_similarity(new_embedding, row_embedding)
        results.append({"id": row_id, "text": row_text, "similarity": round(sim, 4)})
    
    conn.close()
    results.sort(key=lambda x: x["similarity"], reverse=True)
    return results
