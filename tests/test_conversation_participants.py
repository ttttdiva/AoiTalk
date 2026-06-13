import uuid

from src.memory.models import ConversationMessage, ConversationParticipant, ConversationSession


def test_conversation_session_serializes_loaded_participants():
    session_id = uuid.uuid4()
    session = ConversationSession(
        id=session_id,
        user_id="owner-user",
        character_name="group",
        is_group_chat=True,
        group_character_names=["aoi"],
    )
    participant = ConversationParticipant(
        id=uuid.uuid4(),
        session_id=session_id,
        participant_type="user",
        participant_id="invited-user",
        display_name="招待ユーザー",
        role="member",
        status="joined",
        auto_respond=False,
    )
    session.participants = [participant]

    data = session.to_dict()

    assert data["is_group_chat"] is True
    assert data["group_character_names"] == ["aoi"]
    assert data["participants"][0]["participant_type"] == "user"
    assert data["participants"][0]["participant_id"] == "invited-user"
    assert data["participants"][0]["display_name"] == "招待ユーザー"


def test_conversation_message_serializes_sender_identity():
    message = ConversationMessage(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        role="user",
        content="共有チャットの発言",
        sender_type="user",
        sender_id="user-1",
        sender_display_name="User One",
    )

    data = message.to_dict()

    assert data["sender_type"] == "user"
    assert data["sender_id"] == "user-1"
    assert data["sender_display_name"] == "User One"
