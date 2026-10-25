from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import main


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    main.Base.metadata.create_all(engine)
    test_sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def override_database():
        with test_sessions() as session:
            yield session

    monkeypatch.setattr(main, "AUTH_SECRET", "test-secret-that-is-only-used-by-the-test")
    monkeypatch.setattr(main, "SessionLocal", test_sessions)
    main.app.dependency_overrides[main.get_db] = override_database
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()
    engine.dispose()


def register(client: TestClient, username: str, email: str) -> tuple[str, dict]:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "email": email, "password": "correct-horse-battery"},
    )
    assert response.status_code == 201
    result = response.json()
    return result["access_token"], result["user"]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": " ".join(("Bearer", token))}


def test_only_message_author_can_delete_message(client: TestClient) -> None:
    alice_token, alice = register(client, "alice", "alice@example.com")
    bob_token, _ = register(client, "bob", "bob@example.com")
    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    bob_headers = {"Authorization": f"Bearer {bob_token}"}
    conversation = client.post(
        "/api/conversations/direct",
        headers=alice_headers,
        json={"username": "bob"},
    ).json()
    with client.websocket_connect("/ws") as socket:
        socket.send_json({
            "token": bob_token,
            "conversation_id": conversation["id"],
        })
        assert socket.receive_json()["type"] == "connected"

        sent = client.post(
            f"/api/conversations/{conversation['id']}/messages",
            headers=alice_headers,
            json={"content": "Private message"},
        )
        assert sent.status_code == 201
        message_id = sent.json()["id"]
        event = socket.receive_json()
        assert event["type"] == "message_created"
        assert event["message"]["id"] == message_id

        denied = client.delete(f"/api/messages/{message_id}", headers=bob_headers)
        assert denied.status_code == 403

        deleted = client.delete(f"/api/messages/{message_id}", headers=alice_headers)
        assert deleted.status_code == 204
        assert socket.receive_json()["type"] == "message_deleted"

    history = client.get(
        f"/api/conversations/{conversation['id']}/messages",
        headers=bob_headers,
    ).json()
    assert len(history) == 1
    assert history[0]["author"]["id"] == str(alice["id"])
    assert history[0]["content"] is None
    assert history[0]["deleted_at"] is not None


def test_account_deletion_requires_password_and_removes_personal_data(client: TestClient) -> None:
    alice_token, _ = register(client, "alice", "alice@example.com")
    bob_token, bob = register(client, "bob", "bob@example.com")
    carol_token, _ = register(client, "carol", "carol@example.com")
    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    bob_headers = {"Authorization": f"Bearer {bob_token}"}

    direct = client.post(
        "/api/conversations/direct",
        headers=alice_headers,
        json={"username": "bob"},
    ).json()
    group = client.post(
        "/api/conversations/group",
        headers=alice_headers,
        json={"name": "Project team", "usernames": ["bob", "carol"]},
    ).json()
    alice_group_message = client.post(
        f"/api/conversations/{group['id']}/messages",
        headers=alice_headers,
        json={"content": "Alice's message"},
    ).json()
    bob_group_message = client.post(
        f"/api/conversations/{group['id']}/messages",
        headers=bob_headers,
        json={"content": "Bob's message"},
    ).json()
    client.post(
        f"/api/messages/{bob_group_message['id']}/reactions",
        headers=alice_headers,
        json={"emoji": "👍"},
    )

    rejected = client.request(
        "DELETE",
        "/api/auth/account",
        headers=alice_headers,
        json={"password": "wrong-password", "confirmation": "УДАЛИТЬ"},
    )
    assert rejected.status_code == 403
    invalid_confirmation = client.request(
        "DELETE",
        "/api/auth/account",
        headers=alice_headers,
        json={"password": "correct-horse-battery", "confirmation": "удалить"},
    )
    assert invalid_confirmation.status_code == 422

    deleted = client.request(
        "DELETE",
        "/api/auth/account",
        headers=alice_headers,
        json={"password": "correct-horse-battery", "confirmation": "УДАЛИТЬ"},
    )
    assert deleted.status_code == 204
    assert client.get("/api/auth/me", headers=alice_headers).status_code == 401

    remaining_conversations = client.get("/api/conversations", headers=bob_headers).json()
    assert [item["id"] for item in remaining_conversations] == [group["id"]]
    assert {member["username"] for member in remaining_conversations[0]["members"]} == {"bob", "carol"}
    with main.SessionLocal() as db:
        remaining_group = db.get(main.Conversation, UUID(group["id"]))
        assert str(remaining_group.created_by) in {member["id"] for member in remaining_conversations[0]["members"]}
    history = client.get(
        f"/api/conversations/{group['id']}/messages",
        headers=bob_headers,
    ).json()
    assert [item["id"] for item in history] == [bob_group_message["id"]]
    assert history[0]["id"] != alice_group_message["id"]
    assert history[0]["reactions"] == []
    assert client.get(
        f"/api/conversations/{direct['id']}/messages",
        headers=bob_headers,
    ).status_code == 404
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {carol_token}"}).status_code == 200


def test_user_inbox_receives_new_conversation_and_messages(client: TestClient) -> None:
    alice_token, alice = register(client, "alice", "alice@example.com")
    bob_token, bob = register(client, "bob", "bob@example.com")
    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    bob_headers = {"Authorization": f"Bearer {bob_token}"}

    with client.websocket_connect("/ws") as alice_socket:
        alice_socket.send_json({"token": alice_token})
        assert alice_socket.receive_json()["type"] == "connected"

        conversation_response = client.post(
            "/api/conversations/direct",
            headers=bob_headers,
            json={"username": alice["username"]},
        )
        assert conversation_response.status_code == 200
        created_event = alice_socket.receive_json()
        assert created_event["type"] == "conversation_created"
        assert created_event["conversation"]["id"] == conversation_response.json()["id"]
        assert created_event["conversation"]["name"] == bob["username"]

        sent = client.post(
            f"/api/conversations/{conversation_response.json()['id']}/messages",
            headers=bob_headers,
            json={"content": "Hello in a brand-new chat"},
        )
        assert sent.status_code == 201
        event = alice_socket.receive_json()
        assert event["type"] == "message_created"
        assert event["message"]["content"] == "Hello in a brand-new chat"

        edited = client.patch(
            f"/api/messages/{sent.json()['id']}",
            headers=bob_headers,
            json={"content": "Edited immediately"},
        )
        assert edited.status_code == 200
        assert alice_socket.receive_json()["type"] == "message_updated"

        reaction = client.post(
            f"/api/messages/{sent.json()['id']}/reactions",
            headers=alice_headers,
            json={"emoji": "🔥"},
        )
        assert reaction.status_code == 200
        assert alice_socket.receive_json()["type"] == "reaction_updated"

        deleted = client.request(
            "DELETE",
            "/api/auth/account",
            headers=bob_headers,
            json={"password": "correct-horse-battery", "confirmation": "УДАЛИТЬ"},
        )
        assert deleted.status_code == 204
        deletion_event = alice_socket.receive_json()
        assert deletion_event == {
            "type": "conversation_deleted",
            "conversation_id": conversation_response.json()["id"],
        }


def test_sync_returns_new_chats_messages_and_reactions(client: TestClient) -> None:
    alice_token, alice = register(client, "alice", "alice@example.com")
    bob_token, _ = register(client, "bob", "bob@example.com")
    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    bob_headers = {"Authorization": f"Bearer {bob_token}"}

    initial = client.get(
        "/api/sync",
        headers=alice_headers,
        params={"since": "2000-01-01T00:00:00Z"},
    )
    assert initial.status_code == 200
    cursor = initial.json()["cursor"]
    conversation = client.post(
        "/api/conversations/direct",
        headers=bob_headers,
        json={"username": alice["username"]},
    ).json()
    sent = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        headers=bob_headers,
        json={"content": "Message received while offline"},
    ).json()
    client.post(
        f"/api/messages/{sent['id']}/reactions",
        headers=alice_headers,
        json={"emoji": "👍"},
    )

    synced = client.get("/api/sync", headers=alice_headers, params={"since": cursor})
    assert synced.status_code == 200
    assert [item["id"] for item in synced.json()["conversations"]] == [conversation["id"]]
    synced_message = next(item for item in synced.json()["messages"] if item["id"] == sent["id"])
    assert synced_message["content"] == "Message received while offline"
    assert synced_message["reactions"][0]["emoji"] == "👍"
    assert synced_message["reactions"][0]["reacted"] is True


def test_attachment_upload_is_private_and_file_only_send_is_idempotent(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StorageStub:
        def generate_presigned_url(self, operation, Params, ExpiresIn):
            return f"https://storage.example/{operation}/{Params['Key']}"

        def head_object(self, Bucket, Key):
            return {"ContentLength": 1024, "ContentType": "image/png"}

        def delete_objects(self, **kwargs):
            return None

    monkeypatch.setattr(main, "storage_client", StorageStub)
    alice_token, _ = register(client, "alice", "alice@example.com")
    bob_token, _ = register(client, "bob", "bob@example.com")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {alice_token}"}).status_code == 200
    alice_headers = {"Authorization": f"******"}
    bob_headers = {"Authorization": f"******"}
    alice_headers = auth_headers(alice_token)
    bob_headers = auth_headers(bob_token)
    assert client.get("/api/auth/me", headers=alice_headers).status_code == 200
    conversation_response = client.post(
        "/api/conversations/direct",
        headers=alice_headers,
        json={"username": "bob"},
    )
    assert conversation_response.status_code == 200, conversation_response.text
    conversation = conversation_response.json()

    upload = client.post(
        f"/api/conversations/{conversation['id']}/attachments",
        headers=alice_headers,
        json={"filename": "photo.png", "content_type": "image/png", "size": 1024},
    )
    assert upload.status_code == 201
    upload_data = upload.json()
    assert upload_data["headers"] == {"Content-Type": "image/png"}
    assert upload_data["attachment"]["filename"] == "photo.png"
    assert upload_data["attachment"]["url"].startswith("https://storage.example/get_object/")

    incomplete_send = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        headers=alice_headers,
        json={"content": "", "attachment_ids": [upload_data["id"]]},
    )
    assert incomplete_send.status_code == 409

    completed = client.post(
        f"/api/attachments/{upload_data['id']}/complete",
        headers=alice_headers,
    )
    assert completed.status_code == 200
    assert completed.json()["id"] == upload_data["id"]

    forbidden_send = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        headers=bob_headers,
        json={"content": "", "attachment_ids": [upload_data["id"]]},
    )
    assert forbidden_send.status_code == 409

    client_id = "f61d817b-4d77-4709-8b09-8021de91dfe2"
    sent = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        headers=alice_headers,
        json={"content": "", "attachment_ids": [upload_data["id"]], "client_id": client_id},
    )
    assert sent.status_code == 201
    assert sent.json()["content"] is None
    assert sent.json()["attachments"][0]["id"] == upload_data["id"]

    retry = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        headers=alice_headers,
        json={"content": "", "attachment_ids": [upload_data["id"]], "client_id": client_id},
    )
    assert retry.status_code == 201
    assert retry.json()["id"] == sent.json()["id"]
    assert client.get(
        f"/api/conversations/{conversation['id']}/messages",
        headers=bob_headers,
    ).json()[0]["attachments"][0]["filename"] == "photo.png"

    outsider_token, _ = register(client, "carol", "carol@example.com")
    outsider_upload = client.post(
        f"/api/conversations/{conversation['id']}/attachments",
        headers=auth_headers(outsider_token),
        json={"filename": "secret.txt", "content_type": "text/plain", "size": 12},
    )
    assert outsider_upload.status_code == 404


def test_attachment_upload_supports_other_types_and_enforces_50_mib_limit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StorageStub:
        actual_size = main.MAX_UPLOAD_SIZE
        content_type = ""
        deleted = []

        def generate_presigned_url(self, operation, Params, ExpiresIn):
            if operation == "put_object":
                self.content_type = Params["ContentType"]
            return f"https://storage.example/{operation}/{Params['Key']}"

        def head_object(self, Bucket, Key):
            return {"ContentLength": self.actual_size, "ContentType": self.content_type}

        def delete_objects(self, **kwargs):
            self.deleted.extend(item["Key"] for item in kwargs["Delete"]["Objects"])

    storage = StorageStub()
    monkeypatch.setattr(main, "storage_client", lambda: storage)
    alice_token, _ = register(client, "alice", "alice@example.com")
    register(client, "bob", "bob@example.com")
    alice_headers = auth_headers(alice_token)
    conversation = client.post(
        "/api/conversations/direct",
        headers=alice_headers,
        json={"username": "bob"},
    ).json()
    max_size = main.MAX_UPLOAD_SIZE
    office_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    upload = client.post(
        f"/api/conversations/{conversation['id']}/attachments",
        headers=alice_headers,
        json={"filename": "report.docx", "content_type": office_type, "size": max_size},
    )
    assert upload.status_code == 201
    assert upload.json()["attachment"]["content_type"] == office_type
    assert upload.json()["headers"] == {"Content-Type": office_type}

    completed = client.post(
        f"/api/attachments/{upload.json()['id']}/complete",
        headers=alice_headers,
    )
    assert completed.status_code == 200
    assert completed.json()["size"] == max_size

    too_large = client.post(
        f"/api/conversations/{conversation['id']}/attachments",
        headers=alice_headers,
        json={"filename": "large.bin", "content_type": "application/octet-stream", "size": max_size + 1},
    )
    assert too_large.status_code == 422

    storage.actual_size = max_size + 1
    oversized_upload = client.post(
        f"/api/conversations/{conversation['id']}/attachments",
        headers=alice_headers,
        json={"filename": "oversized.bin", "content_type": "application/octet-stream", "size": 1},
    )
    assert oversized_upload.status_code == 201
    oversized_id = oversized_upload.json()["id"]
    rejected = client.post(
        f"/api/attachments/{oversized_id}/complete",
        headers=alice_headers,
    )
    assert rejected.status_code == 413
    assert len(storage.deleted) == 1
    assert client.post(
        f"/api/attachments/{oversized_id}/complete",
        headers=alice_headers,
    ).status_code == 404


def test_api_documentation_routes_follow_environment(client: TestClient) -> None:
    if main.ENVIRONMENT == "development":
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
    else:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404
