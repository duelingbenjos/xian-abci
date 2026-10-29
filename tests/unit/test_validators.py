import unittest

from xian.validators import ValidatorHandler


class _FakeDriver:
    def __init__(self, current_values, committed_values=None):
        self._current_values = dict(current_values)
        self._committed_values = (
            dict(committed_values)
            if committed_values is not None
            else dict(current_values)
        )

    def get(self, key):
        return self._current_values.get(key)

    def value_from_disk(self, key):
        return self._committed_values.get(key)


class _FakeClient:
    def __init__(self, current_values, committed_values=None):
        self.raw_driver = _FakeDriver(current_values, committed_values)


class _FakeApp:
    def __init__(self, current_values, committed_values=None):
        self.client = _FakeClient(current_values, committed_values)


class ValidatorHandlerTests(unittest.TestCase):
    def test_build_validator_updates_adds_new_validator_with_configured_power(self):
        validator = "11" * 32
        stale_validator = "22" * 32
        handler = ValidatorHandler(
            _FakeApp(
                current_values={
                    "validators.active_validators": [validator],
                    f"validators.powers:{validator}": 25,
                },
                committed_values={
                    "validators.active_validators": [stale_validator],
                    f"validators.powers:{stale_validator}": 10,
                },
            )
        )

        updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0].power, 25)
        self.assertEqual(updates[1].power, 0)

    def test_build_validator_updates_changes_existing_power(self):
        validator = "11" * 32
        handler = ValidatorHandler(
            _FakeApp(
                current_values={
                    "validators.active_validators": [validator],
                    f"validators.powers:{validator}": 42,
                },
                committed_values={
                    "validators.active_validators": [validator],
                    f"validators.powers:{validator}": 10,
                },
            )
        )

        updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].power, 42)

    def test_build_validator_updates_falls_back_to_default_power(self):
        validator = "11" * 32
        handler = ValidatorHandler(
            _FakeApp(
                current_values={
                    "validators.active_validators": [validator],
                },
                committed_values={"validators.active_validators": []},
            )
        )

        updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].power, 10)

    def test_build_validator_updates_removes_validator_missing_from_current_state(self):
        validator = "11" * 32
        handler = ValidatorHandler(
            _FakeApp(
                current_values={"validators.active_validators": []},
                committed_values={
                    "validators.active_validators": [validator],
                    f"validators.powers:{validator}": 10,
                },
            )
        )

        updates = handler.build_validator_updates(height=10)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].power, 0)

    def test_build_validator_updates_skips_invalid_validator_keys(self):
        validator = "11" * 32
        handler = ValidatorHandler(
            _FakeApp(
                current_values={
                    "validators.active_validators": [
                        validator,
                        "con_some_contract",
                        "AB" * 32,
                        "11" * 31,
                        7,
                    ],
                },
                committed_values={"validators.active_validators": [validator]},
            )
        )

        self.assertEqual(handler.build_validator_updates(height=10), [])
