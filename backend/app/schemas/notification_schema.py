from app.schemas.base import CamelModel


class NotificationOut(CamelModel):
    id: str
    title: str
    message: str
    type: str  # voice | credential | config | system | student | session | assessment | activity
    created_at: str
    is_read: bool
    link: str | None = None


class NotificationListOut(CamelModel):
    notifications: list[NotificationOut]
    unread_count: int
