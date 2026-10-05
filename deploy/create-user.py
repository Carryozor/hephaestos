import asyncio
import sys
from pathlib import Path

import bcrypt

from app.auth import MAX_PASSWORD_BYTES
from app.storage import Store


async def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: python create-user.py <username> <password>", file=sys.stderr)
        return 1
    username, password = sys.argv[1], sys.argv[2]
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        print(f"Erreur: mot de passe trop long ({len(password.encode())} octets UTF-8, "
              f"max {MAX_PASSWORD_BYTES} : limite de bcrypt).", file=sys.stderr)
        return 1
    data_dir = Path("/data")
    store = Store(data_dir / "state.json")
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        await store.create_user(username, password_hash)
    except ValueError as e:
        print(f"Erreur: {e}", file=sys.stderr)
        return 1
    print(f"Utilisateur '{username}' cree.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
