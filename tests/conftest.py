"""Point every default under /data at a throwaway directory before the app is imported, so no
test can write to (or depend on) the real data volume, whatever the OS."""

import os
import tempfile

_DATA = tempfile.mkdtemp(prefix="wamcp-test-data-")
for name, default in {
    "WACLI_STORE_DIR": "store",
    "MEDIA_DIR": "media",
    "MCP_TOKEN_FILE": "auth_token",
    "WA_CONFIG_FILE": "config.json",
    "ADMIN_PASSWORD_FILE": "admin_password",
    "SESSION_KEY_FILE": "session_key",
}.items():
    os.environ.setdefault(name, os.path.join(_DATA, default))
