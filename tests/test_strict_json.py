import json

import pytest

from api._strict_json import duplicate_key_rejecting_hook


class StrictJsonError(RuntimeError):
    pass


def test_duplicate_key_hook_preserves_objects_and_rejects_duplicates() -> None:
    hook = duplicate_key_rejecting_hook(StrictJsonError, "duplicate key")

    assert json.loads('{"outer":{"value":1}}', object_pairs_hook=hook) == {"outer": {"value": 1}}
    with pytest.raises(StrictJsonError, match="duplicate key"):
        json.loads('{"value":1,"value":2}', object_pairs_hook=hook)
