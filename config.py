import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
GOOGLE_DRIVE_FOLDER_ID = os.environ["GOOGLE_DRIVE_FOLDER_ID"]
AUTH_MODE = os.environ.get("AUTH_MODE", "service_account")

SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:3000/auth/callback")

PORT = int(os.environ.get("PORT", "3000"))

assert BOT_TOKEN, "Missing BOT_TOKEN"
assert GOOGLE_DRIVE_FOLDER_ID, "Missing GOOGLE_DRIVE_FOLDER_ID"
assert AUTH_MODE in ("service_account", "oauth"), "AUTH_MODE must be service_account or oauth"

if AUTH_MODE == "service_account":
    assert SERVICE_ACCOUNT_JSON, "Missing GOOGLE_SERVICE_ACCOUNT_JSON"
