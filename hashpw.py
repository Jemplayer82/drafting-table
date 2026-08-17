"""python -m hashpw -- generates ADMIN_PASSWORD_HASH for .env. Never prints the
raw password back, prompts twice to catch typos, uses getpass so it's not echoed
or left in shell history."""

from __future__ import annotations

import getpass
import sys

from auth import hash_password


def main() -> int:
    pw1 = getpass.getpass("New admin password: ")
    pw2 = getpass.getpass("Confirm: ")
    if pw1 != pw2:
        print("Passwords didn't match.", file=sys.stderr)
        return 1
    if len(pw1) < 12:
        print(
            "Use at least 12 characters -- this guards a public internet endpoint.",
            file=sys.stderr,
        )
        return 1
    print()
    print("ADMIN_PASSWORD_HASH=" + hash_password(pw1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
