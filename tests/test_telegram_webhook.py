from fastapi.testclient import TestClient
from app import app
import config

client = TestClient(app)

def test_telegram_webhook_unauthorized():
    # Missing secret token
    response = client.post("/telegram/webhook", json={"message": "hello"})
    assert response.status_code == 401

    # Wrong secret token
    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong_secret"},
        json={"message": "hello"}
    )
    assert response.status_code == 401

def test_telegram_webhook_payload_too_large():
    # Payload over 2MB limit
    large_payload = "a" * (3 * 1024 * 1024)
    response = client.post(
        "/telegram/webhook",
        headers={
            "X-Telegram-Bot-Api-Secret-Token": config.TELEGRAM_WEBHOOK_SECRET,
            "Content-Length": str(len(large_payload))
        },
        json={"message": large_payload}
    )
    assert response.status_code == 413
