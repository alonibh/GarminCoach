import json

from scripts.secure_telegram_webhook import secure_webhook


def test_rotates_secret_and_registers_without_exposing_it(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TELEGRAM_BOT_TOKEN=bot-token\nTELEGRAM_WEBHOOK_SECRET=old-default\nKEEP=value\n",
        encoding="utf-8",
    )
    requests = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self): return json.dumps({"ok": True}).encode()

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: requests.append((request, timeout)) or Response(),
    )
    secure_webhook(env_path, "example.test")
    values = dict(line.split("=", 1) for line in env_path.read_text().splitlines())
    assert values["KEEP"] == "value"
    assert values["TELEGRAM_WEBHOOK_SECRET"] not in {"", "old-default"}
    body = json.loads(requests[0][0].data)
    assert body["url"] == "https://example.test/telegram/webhook"
    assert body["secret_token"] == values["TELEGRAM_WEBHOOK_SECRET"]
