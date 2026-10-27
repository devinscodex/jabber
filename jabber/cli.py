"""jabber-admin: manage local accounts from the command line.

    python -m jabber.cli [--data-dir DIR] list
    python -m jabber.cli add USER [--name N] [--email E] [--admin]
    python -m jabber.cli passwd USER
    python -m jabber.cli role USER admin|user
    python -m jabber.cli disable USER | enable USER | delete USER

Passwords are read from a prompt (or JABBER_PASSWORD for scripted use),
never from argv.
"""
import argparse
import getpass
import os
import sys

from . import config
from .auth_local import AuthError, LocalAuth


def _password(confirm=True):
    pw = os.environ.get("JABBER_PASSWORD")
    if pw:
        return pw
    pw = getpass.getpass("Password: ")
    if confirm and getpass.getpass("Again: ") != pw:
        raise AuthError("passwords do not match")
    return pw


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jabber-admin")
    ap.add_argument("--config")
    ap.add_argument("--data-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add"); a.add_argument("user"); a.add_argument("--name", default="")
    a.add_argument("--email", default=""); a.add_argument("--admin", action="store_true")
    for c in ("passwd", "disable", "enable", "delete"):
        sub.add_parser(c).add_argument("user")
    r = sub.add_parser("role"); r.add_argument("user"); r.add_argument("role", choices=("user", "admin"))
    args = ap.parse_args(argv)
    data = args.data_dir or config.load(args.config).data_dir
    auth = LocalAuth(os.path.expanduser(data))
    try:
        if args.cmd == "list":
            for u in auth.list_users():
                print(f"{u['username']:<20} {u['role']:<6} {'DISABLED' if u['disabled'] else 'active':<8} {u['name']} {u['email']}")
        elif args.cmd == "add":
            auth.create_user(args.user, _password(), args.name, args.email, "admin" if args.admin else "user")
        elif args.cmd == "passwd":
            auth.set_password(args.user, _password())
        elif args.cmd == "role":
            auth.set_role(args.user, args.role)
        elif args.cmd == "disable":
            auth.set_disabled(args.user, True)
        elif args.cmd == "enable":
            auth.set_disabled(args.user, False)
        elif args.cmd == "delete":
            auth.delete_user(args.user)
    except AuthError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
