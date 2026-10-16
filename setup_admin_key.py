#!/usr/bin/env python3
"""Generate a local editor key in a private file. Never commit/share it."""
import os
import secrets
from pathlib import Path

path = Path(__file__).resolve().with_name('admin_key.txt')
try:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    print('An admin key already exists. No change made.')
    print('To use it, run: cat admin_key.txt  (do not share the key)')
else:
    with os.fdopen(fd, 'w', encoding='utf-8') as file:
        file.write(secrets.token_urlsafe(32) + '\n')
    print('Private admin key created in admin_key.txt (permissions: 600).')
    print('To view or copy it for the browser Settings screen: cat admin_key.txt')
    print('Do not paste the key into chats, screenshots, or GitHub.')
