def can_start_task(
    *,
    global_running: int,
    user_running: int,
    global_limit: int,
    user_limit: int,
) -> bool:
    values = {
        "global_running": global_running,
        "user_running": user_running,
        "global_limit": global_limit,
        "user_limit": user_limit,
    }
    for name, value in values.items():
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")

    return global_running < global_limit and user_running < user_limit
