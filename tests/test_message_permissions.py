from collections.abc import Iterator

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
