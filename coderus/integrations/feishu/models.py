from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

MessageType = Literal["interactive", "text"]
ReceiveIdType = Literal["chat_id", "open_id", "user_id", "union_id", "email"]


class FeishuConfig(BaseModel):
    app_id: str = Field(min_length=1)
    app_secret: SecretStr = Field(min_length=1)


class TaskCompletedMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    issue: str = Field(min_length=1)
    creator: str = Field(min_length=1)
    pr_url: str = Field(min_length=1)


class SendResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    message_id: str
