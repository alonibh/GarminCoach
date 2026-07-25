import pytest

from process_lock import acquire_process_lock, release_process_lock


def test_process_lock_allows_only_one_local_holder(tmp_path):
    path = tmp_path / "garmincoach.lock"
    first = acquire_process_lock(path)
    try:
        with pytest.raises(RuntimeError):
            acquire_process_lock(path)
    finally:
        release_process_lock(first)
    second = acquire_process_lock(path)
    release_process_lock(second)
    release_process_lock(second)
