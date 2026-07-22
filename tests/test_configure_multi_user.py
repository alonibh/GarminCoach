import json

from scripts.configure_multi_user import configure


def test_configure_multi_user_preserves_existing_values_and_hides_secrets(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SESSION_SECRET=keep-me\nAPP_USERNAME=old\n", encoding="utf-8")
    oauth_path = tmp_path / "oauth.json"
    oauth_path.write_text(json.dumps({"web": {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "redirect_uris": ["https://example.test/auth/google/callback"],
        "javascript_origins": ["https://example.test"],
    }}), encoding="utf-8")

    configure(env_path, oauth_path, "Owner@Example.com", "https://example.test")
    values = dict(
        line.split("=", 1)
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert values["SESSION_SECRET"] == "keep-me"
    assert values["APP_USERNAME"] == ""
    assert values["MULTI_USER_ENABLED"] == "true"
    assert values["OWNER_GOOGLE_EMAIL"] == "owner@example.com"
    assert values["GOOGLE_CLIENT_SECRET"] == "client-secret"
    assert values["DATA_ENCRYPTION_KEY"]
    assert values["LLM_ENABLED"] == "false"
