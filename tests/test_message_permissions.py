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
        assert str(remaining_group.created_by) == bob["id"]
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
