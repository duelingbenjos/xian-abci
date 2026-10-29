from unittest.mock import patch

from contracting.execution.runtime import rt

from xian.shielded_preverify import (
    ShieldedPreverifyStats,
    build_verification_request,
    warm_shielded_proof_cache,
)


class _Driver:
    def __init__(self, values):
        self.values = values

    def get_var(self, contract, variable, arguments=None):
        return self.values.get((contract, variable, tuple(arguments or [])))


def test_warm_shielded_proof_cache_binds_supplied_driver_for_zk_lookup():
    driver = object()
    previous_driver = object()
    request = {
        "vk_id": "demo",
        "proof_hex": "0xabcd",
        "public_inputs": ["0x" + "00" * 32],
    }
    rt.env["__Driver"] = previous_driver

    try:
        with (
            patch(
                "xian.shielded_preverify.zk_bridge.is_available",
                return_value=True,
            ),
            patch(
                "xian.shielded_preverify.build_verification_request",
                return_value=request,
            ),
            patch(
                "xian.shielded_preverify.zk_bridge.warm_verified_proofs"
            ) as warm_verified_proofs,
        ):

            def _warm(requests):
                assert requests == [request]
                assert rt.env.get("__Driver") is driver
                return [True]

            warm_verified_proofs.side_effect = _warm
            stats = warm_shielded_proof_cache(
                driver=driver,
                txs=[{"payload": {"kwargs": {"proof_hex": "0xabcd"}}}],
            )

        assert stats == ShieldedPreverifyStats(
            candidate_count=1,
            verified_count=1,
            failed_count=0,
        )
        assert rt.env.get("__Driver") is previous_driver
    finally:
        rt.env.pop("__Driver", None)


def test_warm_shielded_proof_cache_noops_when_zk_bridge_is_unavailable():
    with patch(
        "xian.shielded_preverify.zk_bridge.is_available", return_value=False
    ):
        stats = warm_shielded_proof_cache(
            driver=object(),
            txs=[{"payload": {"kwargs": {"proof_hex": "0xabcd"}}}],
        )

    assert stats == ShieldedPreverifyStats()


def test_warm_shielded_proof_cache_counts_failed_preverification():
    request = {
        "vk_id": "demo",
        "proof_hex": "0xabcd",
        "public_inputs": ["0x" + "00" * 32],
    }

    with (
        patch(
            "xian.shielded_preverify.zk_bridge.is_available", return_value=True
        ),
        patch(
            "xian.shielded_preverify.build_verification_request",
            return_value=request,
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.warm_verified_proofs",
            return_value=[True, False],
        ),
    ):
        stats = warm_shielded_proof_cache(
            driver=object(),
            txs=[
                {"payload": {"kwargs": {"proof_hex": "0x01"}}},
                {"payload": {"kwargs": {"proof_hex": "0x02"}}},
            ],
        )

    assert stats == ShieldedPreverifyStats(
        candidate_count=2,
        verified_count=1,
        failed_count=1,
    )


def test_warm_shielded_proof_cache_skips_malformed_candidates():
    with (
        patch(
            "xian.shielded_preverify.zk_bridge.is_available", return_value=True
        ),
        patch(
            "xian.shielded_preverify.build_verification_request",
            side_effect=AssertionError("bad shielded payload"),
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.warm_verified_proofs"
        ) as warm_verified_proofs,
    ):
        stats = warm_shielded_proof_cache(
            driver=object(),
            txs=[{"payload": {"kwargs": {"proof_hex": "0x01"}}}],
        )

    assert stats == ShieldedPreverifyStats()
    warm_verified_proofs.assert_not_called()


def test_warm_shielded_proof_cache_never_raises_on_rejected_requests():
    good = {"vk_id": "demo", "proof_hex": "0xabcd", "public_inputs": []}
    bad = {"vk_id": "demo", "proof_hex": "0xzz", "public_inputs": []}

    def warm(requests):
        if any(request is bad for request in requests):
            raise AssertionError("invalid proof_hex")
        return [True for _ in requests]

    with (
        patch(
            "xian.shielded_preverify.zk_bridge.is_available", return_value=True
        ),
        patch(
            "xian.shielded_preverify.build_verification_request",
            side_effect=[good, bad, good],
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.warm_verified_proofs",
            side_effect=warm,
        ),
    ):
        stats = warm_shielded_proof_cache(
            driver=object(),
            txs=[{"payload": {}}, {"payload": {}}, {"payload": {}}],
        )

    assert stats == ShieldedPreverifyStats(
        candidate_count=3,
        verified_count=2,
        failed_count=1,
    )


def _tx(function, kwargs, *, contract="con_token", sender="sender", chain_id="xian-test"):
    return {
        "payload": {
            "contract": contract,
            "function": function,
            "sender": sender,
            "kwargs": kwargs,
        },
        "b_meta": {"chain_id": chain_id},
    }


def test_build_verification_request_for_transfer():
    driver = _Driver({("con_token", "vk_ids", ("transfer",)): "transfer-vk"})
    tx = _tx(
        "transfer_shielded",
        {
            "proof_hex": "0xproof",
            "old_root": "0xroot",
            "input_nullifiers": ["0xnull"],
            "output_commitments": ["0xcommit"],
            "output_payloads": ["0xdata"],
        },
    )

    with (
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_output_payload_hashes",
            return_value=["0xpayloadhash"],
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_transfer_public_inputs",
            return_value=["0xpublic"],
        ) as transfer_inputs,
    ):
        request = build_verification_request(driver, tx)

    assert request == {
        "vk_id": "transfer-vk",
        "proof_hex": "0xproof",
        "public_inputs": ["0xpublic"],
    }
    transfer_inputs.assert_called_once_with(
        "con_token",
        "0xroot",
        ["0xnull"],
        ["0xcommit"],
        ["0xpayloadhash"],
    )


def test_build_verification_request_for_withdraw():
    driver = _Driver({("con_token", "vk_ids", ("withdraw",)): "withdraw-vk"})
    tx = _tx(
        "withdraw_shielded",
        {
            "proof_hex": "0xproof",
            "old_root": "0xroot",
            "amount": 7,
            "to": "recipient",
            "input_nullifiers": ["0xnull"],
            "output_commitments": ["0xcommit"],
        },
    )

    with (
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_output_payload_hashes",
            return_value=["0xpayloadhash"],
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_withdraw_public_inputs",
            return_value=["0xpublic"],
        ) as withdraw_inputs,
    ):
        request = build_verification_request(driver, tx)

    assert request == {
        "vk_id": "withdraw-vk",
        "proof_hex": "0xproof",
        "public_inputs": ["0xpublic"],
    }
    withdraw_inputs.assert_called_once_with(
        "con_token",
        "0xroot",
        7,
        "recipient",
        ["0xnull"],
        ["0xcommit"],
        ["0xpayloadhash"],
    )


def test_build_verification_request_for_relay_transfer_binds_chain_and_sender():
    driver = _Driver(
        {("con_token", "vk_ids", ("relay_transfer",)): "relay-vk"}
    )
    tx = _tx(
        "relay_transfer_shielded",
        {
            "proof_hex": "0xproof",
            "old_root": "0xroot",
            "input_nullifiers": ["0xnull"],
            "output_commitments": ["0xcommit"],
            "relayer_fee": 3,
        },
    )

    with (
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_output_payload_hashes",
            side_effect=lambda payloads: [f"hash:{p}" for p in payloads],
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_command_nullifier_digest",
            return_value="0xdigest",
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_command_binding",
            return_value="0xbinding",
        ) as binding,
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_command_execution_tag",
            return_value="0xtag",
        ) as execution_tag,
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_command_public_inputs",
            return_value=["0xpublic"],
        ) as command_inputs,
    ):
        request = build_verification_request(driver, tx)

    assert request == {
        "vk_id": "relay-vk",
        "proof_hex": "0xproof",
        "public_inputs": ["0xpublic"],
    }
    binding_args = binding.call_args.args
    assert binding_args[0] == "0xdigest"
    # No expiry was supplied, so the binding must use the zero field.
    assert binding_args[4] == "0x" + "00" * 32
    assert binding_args[8] == 3
    assert binding_args[9] == 0
    execution_tag.assert_called_once_with("0xdigest", "0xbinding")
    command_inputs.assert_called_once_with(
        "con_token",
        "0xroot",
        "0xbinding",
        "0xtag",
        3,
        0,
        ["0xnull"],
        ["0xcommit"],
        ["hash:"],
    )


def test_build_verification_request_for_execute_command():
    driver = _Driver({("con_pool", "vk_ids", ("command",)): "command-vk"})
    tx = _tx(
        "execute_command",
        {
            "proof_hex": "0xproof",
            "old_root": "0xroot",
            "input_nullifiers": ["0xnull"],
            "output_commitments": ["0xcommit"],
            "target_contract": "con_dex",
            "payload": {"action": "swap"},
            "relayer_fee": 1,
            "public_amount": 5,
            "expires_at": 123,
        },
        contract="con_pool",
    )

    with (
        patch(
            "xian.shielded_preverify._canonicalize_command_payload",
            return_value='{"action":"swap"}',
        ) as canonicalize,
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_output_payload_hashes",
            side_effect=lambda payloads: [f"hash:{p}" for p in payloads],
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_command_nullifier_digest",
            return_value="0xdigest",
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_command_binding",
            return_value="0xbinding",
        ) as binding,
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_command_execution_tag",
            return_value="0xtag",
        ),
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_command_public_inputs",
            return_value=["0xpublic"],
        ),
    ):
        request = build_verification_request(driver, tx)

    assert request == {
        "vk_id": "command-vk",
        "proof_hex": "0xproof",
        "public_inputs": ["0xpublic"],
    }
    canonicalize.assert_called_once_with({"action": "swap"})
    binding_args = binding.call_args.args
    assert binding_args[8] == 1
    assert binding_args[9] == 5


def test_build_verification_request_returns_none_without_vk_id():
    driver = _Driver({})
    tx = _tx(
        "deposit_shielded",
        {
            "proof_hex": "0xproof",
            "old_root": "0xroot",
            "amount": 10,
            "output_commitments": ["0xcommit"],
        },
    )

    assert build_verification_request(driver, tx) is None


def test_build_verification_request_ignores_unrelated_payloads():
    driver = _Driver({})

    assert build_verification_request(driver, {"payload": "not-a-dict"}) is None
    assert build_verification_request(driver, {"payload": {"kwargs": {}}}) is None
    no_proof = _tx("deposit_shielded", {"old_root": "0xroot"})
    assert build_verification_request(driver, no_proof) is None
    unknown_function = _tx("transfer", {"proof_hex": "0xproof"})
    assert build_verification_request(driver, unknown_function) is None
    missing_sender = _tx("deposit_shielded", {"proof_hex": "0xproof"}, sender="")
    assert build_verification_request(driver, missing_sender) is None


def test_build_verification_request_rejects_payload_count_mismatch():
    driver = _Driver({("con_token", "vk_ids", ("deposit",)): "deposit-vk"})
    tx = _tx(
        "deposit_shielded",
        {
            "proof_hex": "0xproof",
            "old_root": "0xroot",
            "amount": 10,
            "output_commitments": ["0xcommit"],
            "output_payloads": ["0xdata", "0xextra"],
        },
    )

    try:
        build_verification_request(driver, tx)
    except AssertionError as exc:
        assert "output_payloads length" in str(exc)
    else:
        raise AssertionError("expected payload count mismatch to raise")


def test_build_verification_request_for_deposit_defaults_empty_payloads():
    driver = _Driver({("con_token", "vk_ids", ("deposit",)): "deposit-vk"})
    tx = {
        "payload": {
            "contract": "con_token",
            "function": "deposit_shielded",
            "sender": "sender",
            "kwargs": {
                "proof_hex": "0xproof",
                "old_root": "0xroot",
                "amount": 10,
                "output_commitments": ["0xcommit"],
            },
        }
    }

    with (
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_output_payload_hashes",
            return_value=["0xpayloadhash"],
        ) as payload_hashes,
        patch(
            "xian.shielded_preverify.zk_bridge.shielded_deposit_public_inputs",
            return_value=["0xpublic"],
        ) as deposit_inputs,
    ):
        request = build_verification_request(driver, tx)

    assert request == {
        "vk_id": "deposit-vk",
        "proof_hex": "0xproof",
        "public_inputs": ["0xpublic"],
    }
    payload_hashes.assert_called_once_with([""])
    deposit_inputs.assert_called_once_with(
        "con_token",
        "0xroot",
        10,
        ["0xcommit"],
        ["0xpayloadhash"],
    )
