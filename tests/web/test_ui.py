from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from coderus.models import User
from coderus.web.ui import WebUI


def make_request() -> Request:
    return Request(scope={"type": "http", "session": {}, "headers": []})


def test_flash_is_read_once(engine) -> None:
    ui = WebUI()
    request = make_request()

    ui.flash(request, "已保存", "ok")
    first = ui.context(request)
    second = ui.context(request)

    assert first["flash"] == {"message": "已保存", "tone": "ok"}
    assert second["flash"] is None


def test_context_reuses_existing_csrf_token(engine) -> None:
    ui = WebUI()
    request = make_request()

    first = ui.context(request)
    second = ui.context(request)

    assert first["csrf_token"] == second["csrf_token"]


def test_stale_session_version_clears_session(engine) -> None:
    ui = WebUI()
    with Session(engine) as session:
        user = User(username="admin", password_hash="hash", role="admin")
        session.add(user)
        session.commit()

        request = make_request()
        request.session.update(
            {"user_id": user.id, "session_version": user.session_version}
        )
        assert ui.current_user(request, session) is user

        user.session_version += 1
        session.commit()
        assert ui.current_user(request, session) is None
        assert request.session == {}


def test_inactive_user_is_rejected(engine) -> None:
    ui = WebUI()
    with Session(engine) as session:
        user = User(
            username="inactive", password_hash="hash", role="user", is_active=False
        )
        session.add(user)
        session.commit()

        request = make_request()
        request.session.update(
            {"user_id": user.id, "session_version": user.session_version}
        )
        assert ui.current_user(request, session) is None
        assert request.session == {}
