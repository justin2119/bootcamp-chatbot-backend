import json
import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import SessionLocal, get_db
from database.models import Conversation, Message

load_dotenv()

RODIUMAI_URL = "https://api.rodiumai.io/v1/chat/completions"
RODIUMAI_API_KEY = os.getenv("RODIUMAI_API_KEY", "")
AUTHORIZED_MODELS = [
    "rodium/auto",
    "anthropic/claude-sonnet-4-5-20250929",
    "rodiumai/smart",
    "openai/gpt-4o-mini",
]
DEFAULT_MODEL = "rodium/auto"
PREVIEW_LENGTH = 60
LLM_ROLES = {"user", "assistant"}
QUIZ_ROLE = "quiz"
QUIZ_EVERY = 4
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")

app = FastAPI(title="Study Buddy Chatbot")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConversationResponse(BaseModel):
    conversation_id: int


class ConversationSummary(BaseModel):
    id: int
    created_at: datetime
    preview: str | None


class ChatRequest(BaseModel):
    conversation_id: int
    message: str
    model: str | None = None
    temperature: float | None = 0.7
    stream: bool = True


class ChatResponse(BaseModel):
    reply: str
    notification: str | None = None


class MessageResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime


def load_messages(db: Session, conversation_id: int) -> list[Message]:
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return db.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
    ).all()


def build_llm_history(rows: list[Message]) -> list[dict[str, str]]:
    return [{"role": row.role, "content": row.content} for row in rows if row.role in LLM_ROLES]


def quiz_due(rows: list[Message]) -> bool:
    # Include the assistant reply about to be committed.
    assistant_count = sum(row.role == "assistant" for row in rows) + 1
    return assistant_count % QUIZ_EVERY == 0


def make_quiz(reply: str) -> str:
    # A lightweight study prompt linked to the latest answer; quiz-role rows are never sent to the LLM.
    excerpt = " ".join(reply.split())[:180]
    return f"À toi de jouer : formule une question de révision sur cette idée clé : {excerpt}"


def persist_success(conversation_id: int, expected_next_seq: int, user_text: str, reply: str, rows: list[Message]) -> str | None:
    quiz_text = make_quiz(reply) if quiz_due(rows) else None
    with SessionLocal() as session:
        if session.get(Conversation, conversation_id) is None:
            raise RuntimeError("Conversation no longer exists.")
        session.add_all([
            Message(conversation_id=conversation_id, seq=expected_next_seq, role="user", content=user_text),
            Message(conversation_id=conversation_id, seq=expected_next_seq + 1, role="assistant", content=reply),
        ])
        if quiz_text:
            session.add(Message(conversation_id=conversation_id, seq=expected_next_seq + 2, role=QUIZ_ROLE, content=quiz_text))
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise RuntimeError("Conversation updated concurrently; retry the request.") from exc
    return quiz_text


@app.get("/models")
def list_models() -> dict[str, object]:
    return {"models": AUTHORIZED_MODELS, "default": DEFAULT_MODEL}


@app.post("/conversations", status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationResponse:
    conversation = Conversation()
    db.add(conversation)
    db.commit()
    return ConversationResponse(conversation_id=conversation.id)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    rows = db.execute(
        select(Conversation, Message.content)
        .outerjoin(Message, and_(Message.conversation_id == Conversation.id, Message.seq == 1))
        .order_by(Conversation.id.desc())
    ).all()
    return [ConversationSummary(id=conversation.id, created_at=conversation.created_at,
                                preview=content[:PREVIEW_LENGTH] if content else None)
            for conversation, content in rows]


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[MessageResponse]:
    return [MessageResponse(seq=m.seq, role=m.role, content=m.content, created_at=m.created_at)
            for m in load_messages(db, conversation_id)]


@app.post("/chat")
async def chat(req: ChatRequest, db: Session = Depends(get_db)):
    if not RODIUMAI_API_KEY:
        raise HTTPException(status_code=500, detail="RODIUMAI_API_KEY non configurée dans le fichier .env")
    if req.model is not None and req.model not in AUTHORIZED_MODELS:
        raise HTTPException(status_code=400, detail=f"Unauthorized model '{req.model}'. Choose one of: {', '.join(AUTHORIZED_MODELS)}")
    selected_model = req.model or DEFAULT_MODEL
    rows = load_messages(db, req.conversation_id)
    history = build_llm_history(rows)
    next_seq = rows[-1].seq + 1 if rows else 1
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": req.message}]
    headers = {"Authorization": f"Bearer {RODIUMAI_API_KEY}"}

    if not req.stream:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(RODIUMAI_URL, headers=headers, json={"model": selected_model, "messages": messages, "temperature": req.temperature, "max_tokens": 512, "stream": False})
                response.raise_for_status()
                reply = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="The LLM API call failed.") from exc
        try:
            notification = persist_success(req.conversation_id, next_seq, req.message, reply, rows)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return ChatResponse(reply=reply, notification=notification)

    async def stream_generator():
        assembled: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", RODIUMAI_URL, headers=headers,
                                         json={"model": selected_model, "messages": messages, "temperature": req.temperature, "max_tokens": 512, "stream": True}) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        delta = event.get("choices", [{}])[0].get("delta", {}).get("content")
                        if delta:
                            assembled.append(delta)
                            yield "data: " + json.dumps({"text": delta}, ensure_ascii=False) + "\n\n"
            reply = "".join(assembled)
            if not reply:
                raise RuntimeError("The model returned an empty response.")
            notification = persist_success(req.conversation_id, next_seq, req.message, reply, rows)
            if notification:
                yield "data: " + json.dumps({"notification": notification}, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
        except GeneratorExit:
            # Client disconnected: leave the database untouched.
            raise
        except (httpx.HTTPError, RuntimeError, KeyError, IndexError, TypeError, ValueError):
            # Never persist a partial response. The stream is terminated without a success sentinel.
            return

    return StreamingResponse(stream_generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
