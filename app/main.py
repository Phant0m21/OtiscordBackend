import asyncio
import os
import re
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from pwdlib import PasswordHash
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, create_engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

load_dotenv()


def _database_url() -> str | None:
    value = os.getenv("DATABASE_URL", "").strip()
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value or None


DATABASE_URL = _database_url()
AUTH_SECRET = os.getenv("AUTH_SECRET", "")
if DATABASE_URL and len(AUTH_SECRET) < 32:
    raise RuntimeError("Set AUTH_SECRET to a random value with at least 32 characters.")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    username_key: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    tag: Mapped[str] = mapped_column(String(4), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    email_key: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    avatar_color: Mapped[str] = mapped_column(String(16), nullable=False, default="mint")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str | None] = mapped_column(String(80))
    direct_key: Mapped[str | None] = mapped_column(String(73), unique=True)
    created_by: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class ConversationMember(Base):
    __tablename__ = "conversation_members"
    __table_args__ = (UniqueConstraint("conversation_id", "user_id", name="uq_conversation_member"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Reaction(Base):
    __tablename__ = "message_reactions"
    __table_args__ = (UniqueConstraint("message_id", "user_id", "emoji", name="uq_message_reaction_user"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    message_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    emoji: Mapped[str] = mapped_column(String(16), nullable=False)


engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300) if DATABASE_URL else None
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False) if engine else None
password_hash = PasswordHash.recommended()
USERNAME_RE = re.compile(r"^[\w.-]{2,32}$", re.UNICODE)
AVATAR_COLORS = ("mint", "violet", "peach", "blue", "gold")

app = FastAPI(title="Otiscord API", version="0.1.0")
origins = [origin.strip() for origin in os.getenv(
    "FRONTEND_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


class RegisterInput(BaseModel):
    email: EmailStr
    username: Annotated[str, StringConstraints(min_length=2, max_length=32)]
    password: Annotated[str, StringConstraints(min_length=8, max_length=128)]


class LoginInput(BaseModel):
    email: EmailStr
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class DirectInput(BaseModel):
    username: Annotated[str, StringConstraints(min_length=2, max_length=32)]


class GroupInput(BaseModel):
    name: Annotated[str, StringConstraints(min_length=2, max_length=80)]
    usernames: list[Annotated[str, StringConstraints(min_length=2, max_length=32)]] = Field(min_length=1, max_length=49)


class MessageInput(BaseModel):
    content: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class ReactionInput(BaseModel):
    emoji: Annotated[str, StringConstraints(min_length=1, max_length=8)]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    username: str
    tag: str
    avatar_color: str


class AuthOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    user: UserOut


class ReactionOut(BaseModel):
    emoji: str
    count: int
    reacted: bool


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    content: str | None
    created_at: datetime
    edited_at: datetime | None
    deleted_at: datetime | None
    author: UserOut
    reactions: list[ReactionOut] = Field(default_factory=list)


class ConversationOut(BaseModel):
    id: UUID
    kind: str
    name: str
    updated_at: datetime
    members: list[UserOut]
    last_message: MessageOut | None


class UserSearchOut(UserOut):
    pass


def get_db():
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database is not configured. Set DATABASE_URL in backend/.env.")
    with SessionLocal() as db:
        yield db


Db = Annotated[Session, Depends(get_db)]

@app.get("/api/health")
def health():
    return {"status": "ok", "database_configured": engine is not None}


def create_access_token(user_id: UUID) -> str:
    return jwt.encode(
        {"sub": str(user_id), "exp": datetime.now(timezone.utc) + timedelta(hours=24), "type": "access"},
        AUTH_SECRET,
        algorithm="HS256",
    )


def user_from_token(token: str, db: Session) -> User:
    try:
        claims = jwt.decode(token, AUTH_SECRET, algorithms=["HS256"])
        if claims.get("type") != "access":
            raise jwt.InvalidTokenError("Invalid token type")
        user_id = UUID(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired access token.") from exc
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account no longer exists.")
    return user


def current_user(
    db: Db,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to continue.")
    return user_from_token(token, db)


CurrentUser = Annotated[User, Depends(current_user)]


def auth_response(user: User) -> AuthOut:
    return AuthOut(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


@app.post("/api/auth/register", response_model=AuthOut, status_code=201)
def register(data: RegisterInput, db: Db):
    username = data.username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(status_code=422, detail="Имя: 2–32 символа; разрешены буквы, цифры, точка, дефис и подчёркивание.")
    email = str(data.email).strip().lower()
    user = User(
        username=username,
        username_key=username.casefold(),
        email=email,
        email_key=email.casefold(),
        tag=f"{secrets.randbelow(10000):04d}",
        password_hash=password_hash.hash(data.password),
        avatar_color=secrets.choice(AVATAR_COLORS),
    )
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Такое имя пользователя или электронная почта уже заняты.") from exc
    return auth_response(user)


@app.post("/api/auth/login", response_model=AuthOut)
def login(data: LoginInput, db: Db):
    email_key = str(data.email).strip().casefold()
    user = db.scalar(select(User).where(User.email_key == email_key))
    if user is None or not password_hash.verify(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверная почта или пароль.")
    return auth_response(user)


@app.get("/api/auth/me", response_model=UserOut)
def me(user: CurrentUser):
    return user


def membership(db: Session, conversation_id: UUID, user_id: UUID) -> Conversation:
    conversation = db.scalar(
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(Conversation.id == conversation_id, ConversationMember.user_id == user_id)
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Чат не найден.")
    return conversation


def get_message_and_conversation(db: Session, message_id: UUID, user_id: UUID) -> tuple[Message, Conversation]:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Сообщение не найдено.")
    conversation = membership(db, message.conversation_id, user_id)
    return message, conversation


def message_payload(db: Session, message: Message, viewer_id: UUID) -> MessageOut:
    author = db.get(User, message.sender_id)
    reaction_rows = db.execute(
        select(Reaction.emoji, Reaction.user_id).where(Reaction.message_id == message.id)
    ).all()
    reaction_counts: dict[str, int] = defaultdict(int)
    reacted_emojis: set[str] = set()
    for emoji, reactor_id in reaction_rows:
        reaction_counts[emoji] += 1
        if reactor_id == viewer_id:
            reacted_emojis.add(emoji)
    return MessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        content=message.content,
        created_at=message.created_at,
        edited_at=message.edited_at,
        deleted_at=message.deleted_at,
        author=UserOut.model_validate(author),
        reactions=[
            ReactionOut(emoji=emoji, count=count, reacted=emoji in reacted_emojis)
            for emoji, count in reaction_counts.items()
        ],
    )


def message_dict(db: Session, message: Message, viewer_id: UUID) -> dict:
    return jsonable_encoder(message_payload(db, message, viewer_id).model_dump(mode="json"))


def publish(conversation_id: UUID, event: dict) -> None:
    loop = websockets.loop
    if loop is None or not loop.is_running():
        return
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run_coroutine_threadsafe(websockets.broadcast(conversation_id, event), loop)
    else:
        if current_loop is loop:
            loop.create_task(websockets.broadcast(conversation_id, event))
        else:
            asyncio.run_coroutine_threadsafe(websockets.broadcast(conversation_id, event), loop)


@app.get("/api/users/search", response_model=list[UserSearchOut])
def search_users(
    db: Db,
    user: CurrentUser,
    q: Annotated[str, Query(min_length=2, max_length=64)],
):
    query = q.strip().lstrip("@").casefold()
    if not query:
        return []
    return db.scalars(
        select(User)
        .where(User.id != user.id)
        .where((User.username_key.contains(query)) | (User.tag.contains(query)))
        .order_by(User.username_key)
        .limit(20)
    ).all()


def create_direct_conversation(db: Session, user: User, other: User) -> Conversation:
    ordered = sorted((str(user.id), str(other.id)))
    direct_key = ":".join(ordered)
    conversation = db.scalar(select(Conversation).where(Conversation.direct_key == direct_key))
    if conversation is None:
        conversation = Conversation(kind="direct", direct_key=direct_key, created_by=user.id)
        db.add(conversation)
        db.flush()
        db.add_all([
            ConversationMember(conversation_id=conversation.id, user_id=user.id),
            ConversationMember(conversation_id=conversation.id, user_id=other.id),
        ])
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            conversation = db.scalar(select(Conversation).where(Conversation.direct_key == direct_key))
            if conversation is None:
                raise HTTPException(status_code=409, detail="Не удалось создать личный чат. Попробуйте ещё раз.")
    return conversation


@app.post("/api/conversations/direct", response_model=ConversationOut)
def open_direct(data: DirectInput, db: Db, user: CurrentUser):
    other = db.scalar(select(User).where(User.username_key == data.username.strip().casefold()))
    if other is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден.")
    if other.id == user.id:
        raise HTTPException(status_code=422, detail="Нельзя открыть личный чат с самим собой.")
    conversation = create_direct_conversation(db, user, other)
    return conversation_payload(db, conversation, user.id)


@app.post("/api/conversations/group", response_model=ConversationOut, status_code=201)
def create_group(data: GroupInput, db: Db, user: CurrentUser):
    usernames = {item.strip().casefold() for item in data.usernames} - {user.username_key}
    users = list(db.scalars(select(User).where(User.username_key.in_(usernames))).all()) if usernames else []
    if len(users) != len(usernames):
        raise HTTPException(status_code=404, detail="Один или несколько пользователей не найдены.")
    conversation = Conversation(kind="group", name=data.name.strip(), created_by=user.id)
    db.add(conversation)
    db.flush()
    db.add_all([ConversationMember(conversation_id=conversation.id, user_id=member_id) for member_id in {user.id, *(item.id for item in users)}])
    db.commit()
    db.refresh(conversation)
    return conversation_payload(db, conversation, user.id)


def conversation_payload(db: Session, conversation: Conversation, viewer_id: UUID) -> ConversationOut:
    member_ids = db.scalars(
        select(ConversationMember.user_id).where(ConversationMember.conversation_id == conversation.id)
    ).all()
    members = list(db.scalars(select(User).where(User.id.in_(member_ids))).all())
    last_message = db.scalar(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    title = conversation.name or next((item.username for item in members if item.id != viewer_id), "Личный чат")
    return ConversationOut(
        id=conversation.id,
        kind=conversation.kind,
        name=title,
        updated_at=conversation.updated_at,
        members=[UserOut.model_validate(item) for item in members],
        last_message=message_payload(db, last_message, viewer_id) if last_message else None,
    )


@app.get("/api/conversations", response_model=list[ConversationOut])
def list_conversations(db: Db, user: CurrentUser):
    conversations = db.scalars(
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(ConversationMember.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
    ).all()
    return [conversation_payload(db, item, user.id) for item in conversations]


@app.get("/api/conversations/{conversation_id}/messages", response_model=list[MessageOut])
def list_messages(
    conversation_id: UUID,
    db: Db,
    user: CurrentUser,
    before: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    membership(db, conversation_id, user.id)
    query = select(Message).where(Message.conversation_id == conversation_id)
    if before:
        cursor = db.get(Message, before)
        if cursor is None or cursor.conversation_id != conversation_id:
            raise HTTPException(status_code=404, detail="Сообщение для пагинации не найдено.")
        query = query.where(Message.created_at < cursor.created_at)
    messages = db.scalars(query.order_by(Message.created_at.desc()).limit(limit)).all()
    messages.reverse()
    return [message_payload(db, item, user.id) for item in messages]


@app.post("/api/conversations/{conversation_id}/messages", response_model=MessageOut, status_code=201)
def send_message(conversation_id: UUID, data: MessageInput, db: Db, user: CurrentUser):
    conversation = membership(db, conversation_id, user.id)
    message = Message(conversation_id=conversation.id, sender_id=user.id, content=data.content.strip())
    if not message.content:
        raise HTTPException(status_code=422, detail="Сообщение не может быть пустым.")
    conversation.updated_at = datetime.now(timezone.utc)
    db.add(message)
    db.commit()
    db.refresh(message)
    payload = message_dict(db, message, user.id)
    publish(conversation.id, {"type": "message_created", "message": payload})
    return payload


@app.patch("/api/messages/{message_id}", response_model=MessageOut)
def edit_message(message_id: UUID, data: MessageInput, db: Db, user: CurrentUser):
    message, _ = get_message_and_conversation(db, message_id, user.id)
    if message.sender_id != user.id:
        raise HTTPException(status_code=403, detail="Редактировать можно только свои сообщения.")
    if message.deleted_at:
        raise HTTPException(status_code=409, detail="Удалённое сообщение нельзя редактировать.")
    message.content = data.content.strip()
    if not message.content:
        raise HTTPException(status_code=422, detail="Сообщение не может быть пустым.")
    message.edited_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(message)
    payload = message_dict(db, message, user.id)
    publish(message.conversation_id, {"type": "message_updated", "message": payload, "actor_id": str(user.id)})
    return payload


@app.delete("/api/messages/{message_id}", status_code=204)
def delete_message(message_id: UUID, db: Db, user: CurrentUser):
    message, _ = get_message_and_conversation(db, message_id, user.id)
    if message.sender_id != user.id:
        raise HTTPException(status_code=403, detail="Удалять можно только свои сообщения.")
    if not message.deleted_at:
        message.content = None
        message.deleted_at = datetime.now(timezone.utc)
        db.execute(delete(Reaction).where(Reaction.message_id == message.id))
        db.commit()
        publish(message.conversation_id, {"type": "message_deleted", "message_id": str(message.id)})


@app.post("/api/messages/{message_id}/reactions", response_model=MessageOut)
def toggle_reaction(message_id: UUID, data: ReactionInput, db: Db, user: CurrentUser):
    message, _ = get_message_and_conversation(db, message_id, user.id)
    if message.deleted_at:
        raise HTTPException(status_code=409, detail="Нельзя реагировать на удалённое сообщение.")
    reaction = db.scalar(
        select(Reaction).where(
            Reaction.message_id == message.id,
            Reaction.user_id == user.id,
            Reaction.emoji == data.emoji,
        )
    )
    if reaction:
        db.delete(reaction)
    else:
        db.add(Reaction(message_id=message.id, user_id=user.id, emoji=data.emoji))
    db.commit()
    payload = message_dict(db, message, user.id)
    publish(message.conversation_id, {
        "type": "reaction_updated",
        "message": payload,
        "actor_id": str(user.id),
    })
    return payload


class WebSocketManager:
    def __init__(self):
        self.connections: dict[UUID, set[WebSocket]] = defaultdict(set)
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, conversation_id: UUID, socket: WebSocket) -> None:
        self.connections[conversation_id].add(socket)

    def disconnect(self, conversation_id: UUID, socket: WebSocket) -> None:
        self.connections[conversation_id].discard(socket)
        if not self.connections[conversation_id]:
            self.connections.pop(conversation_id, None)

    async def broadcast(self, conversation_id: UUID, event: dict) -> None:
        sockets = list(self.connections.get(conversation_id, ()))
        if not sockets:
            return
        results = await asyncio.gather(*(socket.send_json(event) for socket in sockets), return_exceptions=True)
        for socket, result in zip(sockets, results):
            if isinstance(result, Exception):
                self.disconnect(conversation_id, socket)
                try:
                    await socket.close()
                except Exception:
                    pass


websockets = WebSocketManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    websockets.loop = asyncio.get_running_loop()
    try:
        yield
    finally:
        websockets.loop = None


app.router.lifespan_context = lifespan


@app.websocket("/ws")
async def websocket_endpoint(socket: WebSocket):
    await socket.accept()
    try:
        credentials = await asyncio.wait_for(socket.receive_json(), timeout=10)
        token = credentials.get("token")
        conversation_id = UUID(credentials.get("conversation_id", ""))
        if not isinstance(token, str):
            raise ValueError("Missing access token.")
        if SessionLocal is None:
            await socket.close(code=1013, reason="Database is not configured.")
            return
        with SessionLocal() as db:
            user = user_from_token(token, db)
            membership(db, conversation_id, user.id)
        await websockets.connect(conversation_id, socket)
        await socket.send_json({"type": "connected"})
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    except (asyncio.TimeoutError, ValueError, HTTPException, jwt.PyJWTError):
        try:
            await socket.close(code=1008, reason="Authentication or membership check failed.")
        except RuntimeError:
            pass
    finally:
        if "conversation_id" in locals():
            websockets.disconnect(conversation_id, socket)
