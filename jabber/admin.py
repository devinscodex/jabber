"""Admin API (local accounts): list people, let them in, reset, disable, remove.

New accounts get a one-time temporary password the admin passes on; the person
must choose their own at first sign-in. Nothing here reads anyone's chats.
"""
import re

from . import store
from .auth_local import AuthError, temp_password

_USERS = re.compile(r"^/api/admin/users(?:/([a-z0-9._-]{1,64})/(reset|disable|enable|role|delete))?$")


def handle(h, method, path):
    m = _USERS.match(path)
    if not m:
        return False
    ident = h._identity()
    if not ident:
        h._json(401, {"error": "sign in required"})
        return True
    if ident.role != "admin" or not h.app.local:
        h._json(403, {"error": "admins only"})
        return True
    auth, tel = h.app.local, h.app.telemetry
    me = store.ns_for(ident.id)
    try:
        if m.group(1) is None:
            if method == "GET":
                users = auth.list_users()
                for u in users:
                    u["you"] = u["username"] == _my_username(auth, ident)
                h._json(200, {"users": users})
            elif method == "POST":
                body = h._body()
                if body is None:
                    return True
                tmp = temp_password()
                role = "admin" if body.get("role") == "admin" else "user"
                u = auth.create_user(str(body.get("username", "")), tmp, str(body.get("name", ""))[:80],
                                     str(body.get("email", ""))[:120], role, must_change=True)
                tel.event("admin", action="create", by=me, role=role)
                h._json(200, {"username": str(body.get("username", "")).strip().lower(), "temp_password": tmp})
            else:
                h._json(405, {"error": "method not allowed"})
            return True
        user, action = m.groups()
        if method != "POST":
            h._json(405, {"error": "method not allowed"})
            return True
        body = h._body()
        if body is None:
            return True
        if user == _my_username(auth, ident) and action in ("disable", "delete", "role"):
            raise AuthError("you cannot do that to your own account")
        out = {"ok": True}
        if action == "reset":
            out["temp_password"] = tmp = temp_password()
            auth.set_password(user, tmp, must_change=True)
        elif action == "disable":
            auth.set_disabled(user, True)
        elif action == "enable":
            auth.set_disabled(user, False)
        elif action == "role":
            auth.set_role(user, "admin" if body.get("role") == "admin" else "user")
        elif action == "delete":
            auth.delete_user(user)
        tel.event("admin", action=action, by=me)
        h._json(200, out)
    except AuthError as e:
        h._json(400, {"error": str(e)})
    return True


def _my_username(auth, ident):
    return auth.id_map().get(ident.id, "")
