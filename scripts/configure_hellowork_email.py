"""Create the private HelloWork inbox settings file interactively."""

import getpass
import json
from pathlib import Path


def main() -> None:
    username = input("Dedicated Gmail address: ").strip()
    password = "".join(getpass.getpass("Google app password: ").split())
    if "@" not in username or not password:
        raise SystemExit("A Gmail address and app password are required.")
    path = Path("storage/hellowork-email.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"enabled": True, "username": username, "app_password": password},
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"Saved private settings to {path}")


if __name__ == "__main__":
    main()
