import pytest
from sqlalchemy.exc import IntegrityError

from coderus.models import FeishuBotSettings, FeishuEvent, User


def add_user(session) -> User:
    user = User(username="admin", password_hash="hash", role="admin")
    session.add(user)
    session.flush()
    return user


def test_feishu_bot_settings_table_accepts_only_singleton_id(session) -> None:
    user = add_user(session)
    session.add(
        FeishuBotSettings(
            id=1,
            app_id="cli_test",
            encrypted_app_secret="encrypted",
            enabled=True,
            updated_by=user.id,
        )
    )
    session.commit()

    session.add(
        FeishuBotSettings(
            id=2,
            app_id="cli_other",
            encrypted_app_secret="encrypted",
            enabled=True,
            updated_by=user.id,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_feishu_event_message_id_is_unique(session) -> None:
    session.add(
        FeishuEvent(
            message_id="om_duplicate",
            event_id="evt_first",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_sender",
            command="状态",
            status="queued",
        )
    )
    session.commit()

    session.add(
        FeishuEvent(
            message_id="om_duplicate",
            event_id="evt_second",
            chat_id="oc_group",
            chat_type="group",
            sender_open_id="ou_sender",
            command="状态",
            status="queued",
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
