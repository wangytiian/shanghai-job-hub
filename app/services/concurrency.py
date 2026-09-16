"""Optimistic edit-version checks for shared records."""


class EditConflict(ValueError):
    pass


def assert_version(*, actual: int, expected: int) -> None:
    if actual != expected:
        raise EditConflict("该记录已被其他成员更新，请刷新后比较并重新提交。")
