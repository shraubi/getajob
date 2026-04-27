_pending: dict[int, dict] = {}


def save_pending(message_id: int, data: dict) -> None:
    _pending[message_id] = data


def get_pending(message_id: int) -> dict | None:
    return _pending.get(message_id)


def delete_pending(message_id: int) -> None:
    _pending.pop(message_id, None)
