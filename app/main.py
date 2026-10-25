import asyncio
import os
import re
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

import boto3
import jwt
from dotenv import load_dotenv
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from pwdlib import PasswordHash
from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid, create_engine, delete, func, or_, select
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
ATTACHMENTS_BUCKET = os.getenv("ATTACHMENTS_BUCKET", "otiscord-attachments")
ENVIRONMENT = os.getenv("ENVIRONMENT", "").strip().lower()
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
CONTENT_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
ALLOWED_INLINE_TYPES = {
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "video/mp4",
    "video/ogg",
    "video/webm",
    "video/quicktime",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "audio/mp4",
}
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
    bio: Mapped[str] = mapped_column(String(190), nullable=False, default="")
    birth_date: Mapped[date | None] = mapped_column(Date)
    avatar_key: Mapped[str | None] = mapped_column(String(256))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    is_online: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    message_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    uploader_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    object_key: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


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

documentation_enabled = ENVIRONMENT == "development"
app = FastAPI(
    title="Otiscord API",
    version="0.1.0",
    docs_url="/docs" if documentation_enabled else None,
    redoc_url="/redoc" if documentation_enabled else None,
    openapi_url="/openapi.json" if documentation_enabled else None,
)
origins = [origin.strip() for origin in os.getenv(
    "FRONTEND_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",") if origin.strip()]
origin_regex = os.getenv("FRONTEND_ORIGIN_REGEX") or None
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


class RegisterInput(BaseModel):
    email: EmailStr
    username: Annotated[str, StringConstraints(min_length=2, max_length=32)]
    password: Annotated[str, StringConstraints(min_length=8, max_length=128)]


class LoginInput(BaseModel):
    email: EmailStr
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]


class AccountDeletionInput(BaseModel):
    password: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    confirmation: Annotated[str, StringConstraints(pattern="^УДАЛИТЬ$")]


class DirectInput(BaseModel):
    username: Annotated[str, StringConstraints(min_length=2, max_length=32)]


class ProfileInput(BaseModel):
    bio: Annotated[str, StringConstraints(max_length=190)] = ""
    birth_date: date | None = None


class AvatarUploadInput(BaseModel):
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    content_type: Annotated[str, StringConstraints(pattern="^image/(jpeg|png|webp|avif)$")]
    size: Annotated[int, Field(gt=0, le=5 * 1024 * 1024)]


class AvatarCompleteInput(BaseModel):
    object_key: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class GroupInput(BaseModel):
    name: Annotated[str, StringConstraints(min_length=2, max_length=80)]
    usernames: list[Annotated[str, StringConstraints(min_length=2, max_length=32)]] = Field(min_length=1, max_length=49)


class GroupMembersInput(BaseModel):
    usernames: list[Annotated[str, StringConstraints(min_length=2, max_length=32)]] = Field(min_length=1, max_length=49)


class MessageInput(BaseModel):
    content: Annotated[str, StringConstraints(max_length=4000)] = ""
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=10)
    client_id: UUID | None = None


class AttachmentUploadInput(BaseModel):
    filename: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    content_type: Annotated[str, StringConstraints(min_length=1, max_length=127)]
    size: Annotated[int, Field(ge=0, le=MAX_UPLOAD_SIZE)]


class AttachmentOut(BaseModel):
    id: UUID
    filename: str
    content_type: str
    size: int
    url: str


class ReactionInput(BaseModel):
    emoji: Annotated[str, StringConstraints(min_length=1, max_length=8)]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    username: str
    tag: str
    avatar_color: str
    bio: str = ""
    birth_date: date | None = None
    avatar_url: str | None = None
    last_seen_at: datetime | None = None
    is_online: bool = False


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
    attachments: list[AttachmentOut] = Field(default_factory=list)


class ConversationOut(BaseModel):
    id: UUID
    kind: str
    name: str
    updated_at: datetime
    created_by: UUID
    members: list[UserOut]
    last_message: MessageOut | None


class SyncOut(BaseModel):
    cursor: datetime
    conversations: list[ConversationOut]
    messages: list[MessageOut]


class UserSearchOut(UserOut):
    pass


def get_db():
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database is not configured. Set DATABASE_URL in backend/.env.")
    with SessionLocal() as db:
        yield db


Db = Annotated[Session, Depends(get_db)]


def storage_client():
    required = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL_S3", "AWS_REGION")
    if any(not os.getenv(name) for name in required):
        raise HTTPException(
            status_code=503,
            detail="Загрузка файлов недоступна: владелец приложения ещё не подключил Object Storage к backend.",
        )
    return boto3.client(
        "s3",
        region_name=os.environ["AWS_REGION"],
        endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def user_output(user: User) -> UserOut:
    avatar_url = None
    if user.avatar_key:
        storage = storage_client()
        avatar_url = storage.generate_presigned_url(
            "get_object",
            Params={"Bucket": ATTACHMENTS_BUCKET, "Key": user.avatar_key},
            ExpiresIn=43200,
        )
    last_seen_at = user.last_seen_at
    if last_seen_at and last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
    return UserOut(
        id=user.id,
        username=user.username,
        tag=user.tag,
        avatar_color=user.avatar_color,
        bio=user.bio,
        birth_date=user.birth_date,
        avatar_url=avatar_url,
        last_seen_at=last_seen_at,
        is_online=user.is_online and bool(
            last_seen_at and last_seen_at >= datetime.now(timezone.utc) - timedelta(seconds=75)
        ),
    )


def attachment_payload(storage, attachment: Attachment) -> AttachmentOut:
    disposition = "inline" if attachment.content_type in ALLOWED_INLINE_TYPES else "attachment"
    filename = quote(attachment.filename, safe="")
    url = storage.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": ATTACHMENTS_BUCKET,
            "Key": attachment.object_key,
            "ResponseContentDisposition": f"{disposition}; filename*=UTF-8''{filename}",
            "ResponseContentType": attachment.content_type,
        },
        ExpiresIn=3600,
    )
    return AttachmentOut(
        id=attachment.id,
        filename=attachment.filename,
        content_type=attachment.content_type,
        size=attachment.size,
        url=url,
    )


def delete_storage_objects(object_keys: list[str]) -> None:
    if not object_keys:
        return
    storage = storage_client()
    storage.delete_objects(
        Bucket=ATTACHMENTS_BUCKET,
        Delete={"Objects": [{"Key": key} for key in object_keys], "Quiet": True},
    )


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "database_configured": engine is not None,
        "storage_configured": all(os.getenv(name) for name in (
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL_S3", "AWS_REGION"
        )),
    }


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
    return AuthOut(access_token=create_access_token(user.id), user=user_output(user))


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
    return user_output(user)


@app.patch("/api/users/me", response_model=UserOut)
def update_profile(data: ProfileInput, db: Db, user: CurrentUser):
    if data.birth_date and data.birth_date > date.today():
        raise HTTPException(status_code=422, detail="Дата рождения не может быть в будущем.")
    user.bio = data.bio.strip()
    user.birth_date = data.birth_date
    db.commit()
    db.refresh(user)
    payload = user_output(user)
    publish_user_presence(db, user.id)
    return payload


@app.post("/api/users/me/avatar", status_code=201)
def create_avatar_upload(data: AvatarUploadInput, user: CurrentUser):
    storage = storage_client()
    object_key = f"avatars/{user.id}/{uuid4().hex}"
    upload_url = storage.generate_presigned_url(
        "put_object",
        Params={"Bucket": ATTACHMENTS_BUCKET, "Key": object_key, "ContentType": data.content_type},
        ExpiresIn=600,
    )
    return {"upload_url": upload_url, "object_key": object_key, "headers": {"Content-Type": data.content_type}}


@app.put("/api/users/me/avatar", response_model=UserOut)
def complete_avatar_upload(data: AvatarCompleteInput, db: Db, user: CurrentUser):
    if not data.object_key.startswith(f"avatars/{user.id}/"):
        raise HTTPException(status_code=403, detail="Нельзя изменить этот аватар.")
    storage = storage_client()
    try:
        uploaded = storage.head_object(Bucket=ATTACHMENTS_BUCKET, Key=data.object_key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            raise HTTPException(status_code=409, detail="Изображение ещё не загружено.") from exc
        raise
    if uploaded.get("ContentLength", 0) > 5 * 1024 * 1024:
        delete_storage_objects([data.object_key])
        raise HTTPException(status_code=413, detail="Размер аватара не должен превышать 5 МБ.")
    if not (uploaded.get("ContentType") or "").startswith("image/"):
        delete_storage_objects([data.object_key])
        raise HTTPException(status_code=415, detail="Аватар должен быть изображением.")
    old_key = user.avatar_key
    user.avatar_key = data.object_key
    db.commit()
    db.refresh(user)
    if old_key:
        delete_storage_objects([old_key])
    return user_output(user)


@app.delete("/api/auth/account", status_code=204)
def delete_account(data: AccountDeletionInput, db: Db, user: CurrentUser):
    if not password_hash.verify(data.password, user.password_hash):
        raise HTTPException(status_code=403, detail="Неверный пароль.")

    memberships = db.scalars(
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(ConversationMember.user_id == user.id)
    ).all()
    direct_ids = {item.id for item in memberships if item.kind == "direct"}
    empty_group_ids = set()

    for conversation in memberships:
        if conversation.kind != "group":
            continue
        remaining_member_id = db.scalar(
            select(ConversationMember.user_id)
            .where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.user_id != user.id,
            )
            .limit(1)
        )
        if remaining_member_id is None:
            empty_group_ids.add(conversation.id)
        elif conversation.created_by == user.id:
            conversation.created_by = remaining_member_id

    deleted_conversation_ids = direct_ids | empty_group_ids
    removed_conversation_recipients = [
        (
            conversation.id,
            db.scalars(
                select(ConversationMember.user_id).where(
                    ConversationMember.conversation_id == conversation.id,
                    ConversationMember.user_id != user.id,
                )
            ).all(),
        )
        for conversation in memberships
        if conversation.id in deleted_conversation_ids
    ]
    deleted_message_ids = select(Message.id).where(
        or_(
            Message.sender_id == user.id,
            Message.conversation_id.in_(deleted_conversation_ids),
        )
    )
    attachment_keys = db.scalars(
        select(Attachment.object_key).where(
            or_(
                Attachment.uploader_id == user.id,
                Attachment.conversation_id.in_(deleted_conversation_ids),
            )
        )
    ).all()
    if user.avatar_key:
        attachment_keys.append(user.avatar_key)
    delete_storage_objects(attachment_keys)
    db.execute(
        delete(Reaction).where(
            or_(
                Reaction.user_id == user.id,
                Reaction.message_id.in_(deleted_message_ids),
            )
        )
    )
    db.execute(
        delete(Attachment).where(
            or_(
                Attachment.uploader_id == user.id,
                Attachment.conversation_id.in_(deleted_conversation_ids),
            )
        )
    )
    db.execute(delete(Message).where(Message.id.in_(deleted_message_ids)))
    db.execute(delete(ConversationMember).where(ConversationMember.user_id == user.id))
    if deleted_conversation_ids:
        db.execute(delete(Conversation).where(Conversation.id.in_(deleted_conversation_ids)))
    db.delete(user)
    db.commit()

    for conversation_id, recipient_ids in removed_conversation_recipients:
        publish_users(recipient_ids, {
            "type": "conversation_deleted",
            "conversation_id": str(conversation_id),
        })
    if websockets.loop is not None and websockets.loop.is_running():
        asyncio.run_coroutine_threadsafe(websockets.disconnect_user(user.id), websockets.loop)


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
    return message_payloads(db, [message], viewer_id)[0]


def message_payloads(db: Session, messages: list[Message], viewer_id: UUID) -> list[MessageOut]:
    if not messages:
        return []
    message_ids = [message.id for message in messages]
    author_ids = {message.sender_id for message in messages}
    authors = {
        author.id: author
        for author in db.scalars(select(User).where(User.id.in_(author_ids))).all()
    }
    reactions: dict[UUID, dict[str, list[UUID]]] = defaultdict(lambda: defaultdict(list))
    for message_id, emoji, reactor_id in db.execute(
        select(Reaction.message_id, Reaction.emoji, Reaction.user_id).where(Reaction.message_id.in_(message_ids))
    ):
        reactions[message_id][emoji].append(reactor_id)
    attachments: dict[UUID, list[Attachment]] = defaultdict(list)
    for attachment in db.scalars(
        select(Attachment).where(Attachment.message_id.in_(message_ids)).order_by(Attachment.created_at)
    ).all():
        attachments[attachment.message_id].append(attachment)

    storage = storage_client() if attachments else None
    result = []
    for message in messages:
        author = authors.get(message.sender_id)
        if author is None:
            continue
        result.append(MessageOut(
            id=message.id,
            conversation_id=message.conversation_id,
            content=message.content,
            created_at=message.created_at,
            edited_at=message.edited_at,
            deleted_at=message.deleted_at,
            author=user_output(author),
            reactions=[
                ReactionOut(emoji=emoji, count=len(reactors), reacted=viewer_id in reactors)
                for emoji, reactors in reactions[message.id].items()
            ],
            attachments=[
                attachment_payload(storage, attachment)
                for attachment in attachments[message.id]
            ] if storage is not None else [],
        ))
    return result


def message_dict(db: Session, message: Message, viewer_id: UUID) -> dict:
    return jsonable_encoder(message_payload(db, message, viewer_id).model_dump(mode="json"))


def publish_users(user_ids: list[UUID], event: dict) -> None:
    loop = websockets.loop
    if loop is None or not loop.is_running() or not user_ids:
        return
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run_coroutine_threadsafe(websockets.broadcast(user_ids, event), loop)
    else:
        if current_loop is loop:
            loop.create_task(websockets.broadcast(user_ids, event))
        else:
            asyncio.run_coroutine_threadsafe(websockets.broadcast(user_ids, event), loop)


def publish_conversation(db: Session, conversation_id: UUID, event: dict) -> None:
    user_ids = db.scalars(
        select(ConversationMember.user_id).where(ConversationMember.conversation_id == conversation_id)
    ).all()
    publish_users(user_ids, event)


def publish_new_conversation(db: Session, conversation: Conversation) -> None:
    member_ids = db.scalars(
        select(ConversationMember.user_id).where(ConversationMember.conversation_id == conversation.id)
    ).all()
    for member_id in member_ids:
        payload = conversation_payload(db, conversation, member_id)
        publish_users([member_id], {
            "type": "conversation_created",
            "conversation": jsonable_encoder(payload.model_dump(mode="json")),
        })


def publish_user_presence(db: Session, user_id: UUID) -> None:
    conversation_ids = select(ConversationMember.conversation_id).where(ConversationMember.user_id == user_id)
    recipients = db.scalars(
        select(ConversationMember.user_id)
        .where(ConversationMember.conversation_id.in_(conversation_ids), ConversationMember.user_id != user_id)
        .distinct()
    ).all()
    user = db.get(User, user_id)
    if user is not None:
        publish_users(recipients, {
            "type": "presence_updated",
            "user": jsonable_encoder(user_output(user).model_dump(mode="json")),
        })


@app.get("/api/users/search", response_model=list[UserSearchOut])
def search_users(
    db: Db,
    user: CurrentUser,
    q: Annotated[str, Query(min_length=2, max_length=64)],
):
    query = q.strip().lstrip("@").casefold()
    if not query:
        return []
    users = db.scalars(
        select(User)
        .where(User.id != user.id)
        .where((User.username_key.contains(query)) | (User.tag.contains(query)))
        .order_by(User.username_key)
        .limit(20)
    ).all()
    return [user_output(item) for item in users]


def create_direct_conversation(db: Session, user: User, other: User) -> tuple[Conversation, bool]:
    ordered = sorted((str(user.id), str(other.id)))
    direct_key = ":".join(ordered)
    conversation = db.scalar(select(Conversation).where(Conversation.direct_key == direct_key))
    created = False
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
            created = True
        except IntegrityError:
            db.rollback()
            conversation = db.scalar(select(Conversation).where(Conversation.direct_key == direct_key))
            if conversation is None:
                raise HTTPException(status_code=409, detail="Не удалось создать личный чат. Попробуйте ещё раз.")
    return conversation, created


@app.post("/api/conversations/direct", response_model=ConversationOut)
def open_direct(data: DirectInput, db: Db, user: CurrentUser):
    other = db.scalar(select(User).where(User.username_key == data.username.strip().casefold()))
    if other is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден.")
    if other.id == user.id:
        raise HTTPException(status_code=422, detail="Нельзя открыть личный чат с самим собой.")
    conversation, created = create_direct_conversation(db, user, other)
    if created:
        publish_new_conversation(db, conversation)
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
    publish_new_conversation(db, conversation)
    return conversation_payload(db, conversation, user.id)


def require_group_owner(db: Session, conversation_id: UUID, user_id: UUID) -> Conversation:
    conversation = membership(db, conversation_id, user_id)
    if conversation.kind != "group" or conversation.created_by != user_id:
        raise HTTPException(status_code=403, detail="Управлять группой может только её создатель.")
    return conversation


@app.post("/api/conversations/{conversation_id}/members", response_model=ConversationOut)
def add_group_members(
    conversation_id: UUID,
    data: GroupMembersInput,
    db: Db,
    user: CurrentUser,
):
    conversation = require_group_owner(db, conversation_id, user.id)
    usernames = {name.strip().casefold() for name in data.usernames} - {user.username_key}
    users = db.scalars(select(User).where(User.username_key.in_(usernames))).all() if usernames else []
    if len(users) != len(usernames):
        raise HTTPException(status_code=404, detail="Один или несколько пользователей не найдены.")
    existing_ids = set(db.scalars(
        select(ConversationMember.user_id).where(ConversationMember.conversation_id == conversation.id)
    ).all())
    new_users = [item for item in users if item.id not in existing_ids]
    if len(existing_ids) + len(new_users) > 50:
        raise HTTPException(status_code=422, detail="В группе может быть не более 50 участников.")
    db.add_all(ConversationMember(conversation_id=conversation.id, user_id=item.id) for item in new_users)
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conversation)
    publish_new_conversation(db, conversation)
    return conversation_payload(db, conversation, user.id)


@app.delete("/api/conversations/{conversation_id}/members/{member_id}", status_code=204)
def remove_group_member(conversation_id: UUID, member_id: UUID, db: Db, user: CurrentUser):
    conversation = require_group_owner(db, conversation_id, user.id)
    if member_id == user.id:
        raise HTTPException(status_code=422, detail="Чтобы выйти из группы, используй действие «Выйти из группы».")
    member = db.scalar(select(ConversationMember).where(
        ConversationMember.conversation_id == conversation.id,
        ConversationMember.user_id == member_id,
    ))
    if member is None:
        raise HTTPException(status_code=404, detail="Участник не найден.")
    db.delete(member)
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()
    publish_users([member_id], {"type": "conversation_deleted", "conversation_id": str(conversation.id)})
    publish_new_conversation(db, conversation)


@app.post("/api/conversations/{conversation_id}/leave", status_code=204)
def leave_group(conversation_id: UUID, db: Db, user: CurrentUser):
    conversation = membership(db, conversation_id, user.id)
    if conversation.kind != "group":
        raise HTTPException(status_code=422, detail="Выйти можно только из группы.")
    former_members = db.scalars(
        select(ConversationMember.user_id).where(ConversationMember.conversation_id == conversation.id)
    ).all()
    db.execute(delete(ConversationMember).where(
        ConversationMember.conversation_id == conversation.id,
        ConversationMember.user_id == user.id,
    ))
    remaining = [member_id for member_id in former_members if member_id != user.id]
    if not remaining:
        db.delete(conversation)
        db.commit()
        publish_users(former_members, {"type": "conversation_deleted", "conversation_id": str(conversation_id)})
        return
    if conversation.created_by == user.id:
        conversation.created_by = secrets.choice(remaining)
    conversation.updated_at = datetime.now(timezone.utc)
    db.commit()
    publish_users([user.id], {"type": "conversation_deleted", "conversation_id": str(conversation_id)})
    publish_new_conversation(db, conversation)


@app.delete("/api/conversations/{conversation_id}", status_code=204)
def delete_group(conversation_id: UUID, db: Db, user: CurrentUser):
    conversation = require_group_owner(db, conversation_id, user.id)
    member_ids = db.scalars(
        select(ConversationMember.user_id).where(ConversationMember.conversation_id == conversation.id)
    ).all()
    attachment_keys = db.scalars(
        select(Attachment.object_key).where(Attachment.conversation_id == conversation.id)
    ).all()
    delete_storage_objects(attachment_keys)
    db.delete(conversation)
    db.commit()
    publish_users(member_ids, {"type": "conversation_deleted", "conversation_id": str(conversation_id)})


def conversation_payload(db: Session, conversation: Conversation, viewer_id: UUID) -> ConversationOut:
    return conversation_payloads(db, [conversation], viewer_id)[0]


def conversation_payloads(db: Session, conversations: list[Conversation], viewer_id: UUID) -> list[ConversationOut]:
    if not conversations:
        return []
    conversation_ids = [item.id for item in conversations]
    members: dict[UUID, list[User]] = defaultdict(list)
    for conversation_id, member in db.execute(
        select(ConversationMember.conversation_id, User)
        .join(User, User.id == ConversationMember.user_id)
        .where(ConversationMember.conversation_id.in_(conversation_ids))
    ):
        members[conversation_id].append(member)

    latest_rows = (
        select(
            Message.id.label("message_id"),
            func.row_number().over(
                partition_by=Message.conversation_id,
                order_by=(Message.created_at.desc(), Message.id.desc()),
            ).label("row_number"),
        )
        .where(Message.conversation_id.in_(conversation_ids))
        .subquery()
    )
    latest_messages = db.scalars(
        select(Message)
        .join(latest_rows, Message.id == latest_rows.c.message_id)
        .where(latest_rows.c.row_number == 1)
    ).all()
    payloads = {
        item.id: item
        for item in message_payloads(db, latest_messages, viewer_id)
    }
    latest_by_conversation = {item.conversation_id: payloads.get(item.id) for item in latest_messages}

    return [
        ConversationOut(
            id=conversation.id,
            kind=conversation.kind,
            created_by=conversation.created_by,
            name=conversation.name or next(
                (item.username for item in members[conversation.id] if item.id != viewer_id),
                "Личный чат",
            ),
            updated_at=conversation.updated_at,
            members=[user_output(item) for item in members[conversation.id]],
            last_message=latest_by_conversation.get(conversation.id),
        )
        for conversation in conversations
    ]


@app.get("/api/conversations", response_model=list[ConversationOut])
def list_conversations(db: Db, user: CurrentUser):
    conversations = db.scalars(
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(ConversationMember.user_id == user.id)
        .order_by(Conversation.updated_at.desc())
    ).all()
    return conversation_payloads(db, conversations, user.id)


@app.get("/api/presence", response_model=list[UserOut])
def list_presence(db: Db, user: CurrentUser):
    member_ids = select(ConversationMember.conversation_id).where(ConversationMember.user_id == user.id)
    users = db.scalars(
        select(User)
        .join(ConversationMember, ConversationMember.user_id == User.id)
        .where(
            ConversationMember.conversation_id.in_(member_ids),
            User.id != user.id,
        )
        .distinct()
    ).all()
    return [user_output(member) for member in users]


@app.get("/api/sync", response_model=SyncOut)
def sync_changes(
    db: Db,
    user: CurrentUser,
    since: Annotated[datetime, Query()],
):
    cursor = datetime.now(timezone.utc)
    conversations = db.scalars(
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(ConversationMember.user_id == user.id, Conversation.updated_at >= since)
        .order_by(Conversation.updated_at)
        .limit(200)
    ).all()
    messages = db.scalars(
        select(Message)
        .join(ConversationMember, ConversationMember.conversation_id == Message.conversation_id)
        .where(ConversationMember.user_id == user.id, Message.updated_at >= since)
        .order_by(Message.updated_at)
        .limit(200)
    ).unique().all()
    return SyncOut(
        cursor=cursor,
        conversations=conversation_payloads(db, conversations, user.id),
        messages=message_payloads(db, messages, user.id),
    )


@app.post("/api/conversations/{conversation_id}/attachments", status_code=201)
def create_attachment_upload(
    conversation_id: UUID,
    data: AttachmentUploadInput,
    db: Db,
    user: CurrentUser,
):
    membership(db, conversation_id, user.id)
    storage = storage_client()
    mime_type = data.content_type.strip().lower()
    if not CONTENT_TYPE_RE.fullmatch(mime_type):
        mime_type = "application/octet-stream"
    filename = os.path.basename(data.filename.replace("\\", "/")).strip()
    if not filename or filename in {".", ".."}:
        raise HTTPException(status_code=422, detail="У файла должно быть корректное имя.")
    attachment = Attachment(
        conversation_id=conversation_id,
        uploader_id=user.id,
        object_key=f"attachments/{user.id}/{uuid4().hex}",
        filename=filename,
        content_type=mime_type,
        size=data.size,
    )
    db.add(attachment)
    db.flush()
    upload_url = storage.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": ATTACHMENTS_BUCKET,
            "Key": attachment.object_key,
            "ContentType": mime_type,
        },
        ExpiresIn=600,
    )
    attachment_out = attachment_payload(storage, attachment).model_dump(mode="json")
    db.commit()
    return {
        "id": str(attachment.id),
        "upload_url": upload_url,
        "headers": {"Content-Type": mime_type},
        "attachment": attachment_out,
    }


@app.post("/api/attachments/{attachment_id}/complete", response_model=AttachmentOut)
def complete_attachment_upload(attachment_id: UUID, db: Db, user: CurrentUser):
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="Файл не найден.")
    if attachment.uploader_id != user.id or attachment.message_id is not None:
        raise HTTPException(status_code=403, detail="Нельзя завершить загрузку этого файла.")
    storage = storage_client()
    try:
        uploaded = storage.head_object(Bucket=ATTACHMENTS_BUCKET, Key=attachment.object_key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            raise HTTPException(status_code=409, detail="Файл ещё не загружен полностью.") from exc
        raise
    if uploaded.get("ContentLength", 0) > MAX_UPLOAD_SIZE:
        delete_storage_objects([attachment.object_key])
        db.delete(attachment)
        db.commit()
        raise HTTPException(status_code=413, detail="Размер файла превышает ограничение 50 МБ.")
    if uploaded.get("ContentLength") != attachment.size or uploaded.get("ContentType") != attachment.content_type:
        raise HTTPException(status_code=409, detail="Размер или тип загруженного файла не совпадает с заявленным.")
    if attachment.uploaded_at is None:
        attachment.uploaded_at = datetime.now(timezone.utc)
        db.commit()
    return attachment_payload(storage, attachment)


@app.delete("/api/attachments/{attachment_id}", status_code=204)
def cancel_attachment_upload(attachment_id: UUID, db: Db, user: CurrentUser):
    attachment = db.get(Attachment, attachment_id)
    if attachment is None:
        return
    if attachment.uploader_id != user.id or attachment.message_id is not None:
        raise HTTPException(status_code=403, detail="Нельзя удалить этот файл.")
    delete_storage_objects([attachment.object_key])
    db.delete(attachment)
    db.commit()


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
    return message_payloads(db, messages, user.id)


@app.post("/api/conversations/{conversation_id}/messages", response_model=MessageOut, status_code=201)
def send_message(conversation_id: UUID, data: MessageInput, db: Db, user: CurrentUser):
    conversation = membership(db, conversation_id, user.id)
    content = data.content.strip()
    message_id = data.client_id or uuid4()
    existing_message = db.get(Message, message_id)
    if existing_message is not None:
        if existing_message.conversation_id != conversation.id or existing_message.sender_id != user.id:
            raise HTTPException(status_code=409, detail="Идентификатор сообщения уже используется.")
        return message_dict(db, existing_message, user.id)
    attachments = db.scalars(
        select(Attachment).where(
            Attachment.id.in_(data.attachment_ids),
            Attachment.conversation_id == conversation.id,
            Attachment.uploader_id == user.id,
            Attachment.message_id.is_(None),
            Attachment.uploaded_at.is_not(None),
        )
    ).all() if data.attachment_ids else []
    if len(attachments) != len(set(data.attachment_ids)):
        raise HTTPException(status_code=409, detail="Один из файлов уже отправлен или недоступен.")
    if not content and not attachments:
        raise HTTPException(status_code=422, detail="Сообщение не может быть пустым.")
    now = datetime.now(timezone.utc)
    message = Message(
        id=message_id,
        conversation_id=conversation.id,
        sender_id=user.id,
        content=content or None,
        created_at=now,
        updated_at=now,
    )
    conversation.updated_at = datetime.now(timezone.utc)
    db.add(message)
    for attachment in attachments:
        attachment.message_id = message.id
    db.commit()
    db.refresh(message)
    payload = message_dict(db, message, user.id)
    publish_conversation(db, conversation.id, {"type": "message_created", "message": payload})
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
    message.edited_at = message.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(message)
    payload = message_dict(db, message, user.id)
    publish_conversation(db, message.conversation_id, {"type": "message_updated", "message": payload, "actor_id": str(user.id)})
    return payload


@app.delete("/api/messages/{message_id}", status_code=204)
def delete_message(message_id: UUID, db: Db, user: CurrentUser):
    message, _ = get_message_and_conversation(db, message_id, user.id)
    if message.sender_id != user.id:
        raise HTTPException(status_code=403, detail="Удалять можно только свои сообщения.")
    if not message.deleted_at:
        message.content = None
        message.deleted_at = message.updated_at = datetime.now(timezone.utc)
        attachment_keys = db.scalars(
            select(Attachment.object_key).where(Attachment.message_id == message.id)
        ).all()
        delete_storage_objects(attachment_keys)
        db.execute(delete(Attachment).where(Attachment.message_id == message.id))
        db.execute(delete(Reaction).where(Reaction.message_id == message.id))
        db.commit()
        publish_conversation(db, message.conversation_id, {"type": "message_deleted", "message_id": str(message.id)})


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
    message.updated_at = datetime.now(timezone.utc)
    db.commit()
    payload = message_dict(db, message, user.id)
    publish_conversation(db, message.conversation_id, {
        "type": "reaction_updated",
        "message": payload,
        "actor_id": str(user.id),
    })
    return payload


class WebSocketManager:
    def __init__(self):
        self.connections: dict[UUID, set[WebSocket]] = defaultdict(set)
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, user_id: UUID, socket: WebSocket) -> None:
        self.connections[user_id].add(socket)

    def disconnect(self, user_id: UUID, socket: WebSocket) -> bool:
        self.connections[user_id].discard(socket)
        if not self.connections[user_id]:
            self.connections.pop(user_id, None)
            return True
        return False

    async def disconnect_user(self, user_id: UUID) -> None:
        sockets = list(self.connections.pop(user_id, ()))
        if sockets:
            await asyncio.gather(
                *(socket.close(code=1008, reason="Account deleted.") for socket in sockets),
                return_exceptions=True,
            )

    async def broadcast(self, user_ids: list[UUID], event: dict) -> None:
        sockets = {
            socket
            for user_id in user_ids
            for socket in self.connections.get(user_id, ())
        }
        if not sockets:
            return
        results = await asyncio.gather(*(socket.send_json(event) for socket in sockets), return_exceptions=True)
        for socket, result in zip(sockets, results):
            if isinstance(result, Exception):
                for user_id in user_ids:
                    self.disconnect(user_id, socket)
                try:
                    await socket.close()
                except (WebSocketDisconnect, RuntimeError):
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
    user_id = None
    await socket.accept()
    try:
        credentials = await asyncio.wait_for(socket.receive_json(), timeout=10)
        if not isinstance(credentials, dict):
            raise ValueError("Invalid credentials payload.")
        token = credentials.get("token")
        if not isinstance(token, str):
            raise ValueError("Missing access token.")
        if SessionLocal is None:
            await socket.close(code=1013, reason="Database is not configured.")
            return
        with SessionLocal() as db:
            user = user_from_token(token, db)
            user_id = user.id
            user.is_online = True
            user.last_seen_at = datetime.now(timezone.utc)
            db.commit()
        await websockets.connect(user_id, socket)
        await socket.send_json({"type": "connected"})
        with SessionLocal() as db:
            publish_user_presence(db, user_id)
        while True:
            event = await socket.receive_json()
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "heartbeat":
                with SessionLocal() as db:
                    current = db.get(User, user_id)
                    if current is not None:
                        current.is_online = True
                        current.last_seen_at = datetime.now(timezone.utc)
                        db.commit()
                continue
            if event_type not in {"typing", "typing_stopped"}:
                continue
            try:
                conversation_id = UUID(str(event.get("conversation_id")))
            except ValueError:
                continue
            with SessionLocal() as db:
                conversation = db.get(Conversation, conversation_id)
                if conversation is None or db.scalar(
                    select(ConversationMember.id).where(
                        ConversationMember.conversation_id == conversation_id,
                        ConversationMember.user_id == user_id,
                    )
                ) is None:
                    continue
                username = db.scalar(select(User.username).where(User.id == user_id))
                member_ids = db.scalars(
                    select(ConversationMember.user_id).where(
                        ConversationMember.conversation_id == conversation_id,
                        ConversationMember.user_id != user_id,
                    )
                ).all()
            publish_users(member_ids, {
                "type": event_type,
                "conversation_id": str(conversation_id),
                "user_id": str(user_id),
                "username": username,
            })
    except WebSocketDisconnect:
        pass
    except (asyncio.TimeoutError, ValueError, HTTPException, jwt.PyJWTError):
        try:
            await socket.close(code=1008, reason="Authentication or membership check failed.")
        except RuntimeError:
            pass
    finally:
        if user_id is not None:
            last_connection = websockets.disconnect(user_id, socket)
            if last_connection and SessionLocal is not None:
                with SessionLocal() as db:
                    user = db.get(User, user_id)
                    if user is not None:
                        user.is_online = False
                        user.last_seen_at = datetime.now(timezone.utc)
                        db.commit()
                        publish_user_presence(db, user_id)
