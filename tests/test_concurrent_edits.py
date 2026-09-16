import pytest

from app.services.concurrency import EditConflict, assert_version
from app.services.structuring import StructuringInput, structure_job


def test_assert_version_allows_the_current_edit_version():
    assert_version(actual=3, expected=3)


def test_assert_version_rejects_a_stale_edit_version():
    with pytest.raises(EditConflict, match="已被其他成员更新"):
        assert_version(actual=4, expected=3)
