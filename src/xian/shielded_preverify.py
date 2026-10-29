from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracting.execution.runtime import rt
from contracting.stdlib.bridge import zk as zk_bridge

try:
    from xian_zk import (
        canonicalize_command_payload as _canonicalize_command_payload,
    )
except ImportError:  # pragma: no cover - exercised when zk extras are absent
    _canonicalize_command_payload = None


@dataclass(frozen=True)
class ShieldedPreverifyStats:
    candidate_count: int = 0
    verified_count: int = 0
    failed_count: int = 0


def _contract_var(driver, contract: str, variable: str, arguments: list[str]):
    return driver.get_var(contract, variable, arguments)


def _vk_id_for(driver, contract: str, action: str) -> str | None:
    vk_id = _contract_var(driver, contract, "vk_ids", [action])
    if isinstance(vk_id, str) and vk_id != "":
        return vk_id
    return None


def _normalize_output_payloads(
    output_payloads: Any,
    expected_count: int,
) -> list[str]:
    if output_payloads is None:
        return [""] * expected_count
    if not isinstance(output_payloads, list) or len(output_payloads) != expected_count:
        raise AssertionError("output_payloads length must match output commitments")

    normalized = []
    for payload in output_payloads:
        if payload in (None, ""):
            normalized.append("")
        else:
            normalized.append(payload)
    return normalized


def _payload_hashes(output_payloads: list[str]) -> list[str]:
    return zk_bridge.shielded_output_payload_hashes(output_payloads)


def _text_field_hash(value: str) -> str:
    return _payload_hashes(["0x" + value.encode("utf-8").hex()])[0]


def _expiry_field(expires_at: Any) -> str:
    if expires_at in (None, ""):
        return "0x" + "00" * 32
    return _text_field_hash(str(expires_at))


def _command_payload_digest(payload: Any) -> str:
    if _canonicalize_command_payload is None:
        raise AssertionError("xian_zk command payload canonicalizer is unavailable")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise AssertionError("payload must be a dict")
    canonical = _canonicalize_command_payload(payload)
    return _text_field_hash(canonical)


def _entry_inputs(
    driver,
    contract: str,
    action: str,
    kwargs: dict,
) -> tuple[str, list, list[str]] | None:
    """Resolve the vk gate and output hashing shared by every entry point."""
    vk_id = _vk_id_for(driver, contract, action)
    if vk_id is None:
        return None
    commitments = kwargs["output_commitments"]
    output_payloads = _normalize_output_payloads(
        kwargs.get("output_payloads"),
        len(commitments),
    )
    return vk_id, commitments, _payload_hashes(output_payloads)


def _deposit_entry(driver, contract: str, kwargs: dict) -> dict[str, Any] | None:
    inputs = _entry_inputs(driver, contract, "deposit", kwargs)
    if inputs is None:
        return None
    vk_id, commitments, payload_hashes = inputs
    return {
        "vk_id": vk_id,
        "proof_hex": kwargs["proof_hex"],
        "public_inputs": zk_bridge.shielded_deposit_public_inputs(
            contract,
            kwargs["old_root"],
            kwargs["amount"],
            commitments,
            payload_hashes,
        ),
    }


def _transfer_entry(driver, contract: str, kwargs: dict) -> dict[str, Any] | None:
    inputs = _entry_inputs(driver, contract, "transfer", kwargs)
    if inputs is None:
        return None
    vk_id, commitments, payload_hashes = inputs
    return {
        "vk_id": vk_id,
        "proof_hex": kwargs["proof_hex"],
        "public_inputs": zk_bridge.shielded_transfer_public_inputs(
            contract,
            kwargs["old_root"],
            kwargs["input_nullifiers"],
            commitments,
            payload_hashes,
        ),
    }


def _withdraw_entry(driver, contract: str, kwargs: dict) -> dict[str, Any] | None:
    inputs = _entry_inputs(driver, contract, "withdraw", kwargs)
    if inputs is None:
        return None
    vk_id, commitments, payload_hashes = inputs
    return {
        "vk_id": vk_id,
        "proof_hex": kwargs["proof_hex"],
        "public_inputs": zk_bridge.shielded_withdraw_public_inputs(
            contract,
            kwargs["old_root"],
            kwargs["amount"],
            kwargs["to"],
            kwargs["input_nullifiers"],
            commitments,
            payload_hashes,
        ),
    }


def _command_request(
    *,
    contract: str,
    sender: str,
    chain_id: str,
    kwargs: dict,
    vk_id: str,
    commitments: list,
    payload_hashes: list[str],
    target_hash: str,
    payload_digest: str,
    interaction: str,
    domain: str,
    public_amount: Any,
) -> dict[str, Any]:
    input_nullifiers = kwargs["input_nullifiers"]
    fee = kwargs.get("relayer_fee", 0)
    nullifier_digest = zk_bridge.shielded_command_nullifier_digest(input_nullifiers)
    binding = zk_bridge.shielded_command_binding(
        nullifier_digest,
        target_hash,
        payload_digest,
        _text_field_hash(sender),
        _expiry_field(kwargs.get("expires_at")),
        _text_field_hash(chain_id),
        _text_field_hash(interaction),
        _text_field_hash(domain),
        fee,
        public_amount,
    )
    execution_tag = zk_bridge.shielded_command_execution_tag(
        nullifier_digest,
        binding,
    )
    return {
        "vk_id": vk_id,
        "proof_hex": kwargs["proof_hex"],
        "public_inputs": zk_bridge.shielded_command_public_inputs(
            contract,
            kwargs["old_root"],
            binding,
            execution_tag,
            fee,
            public_amount,
            input_nullifiers,
            commitments,
            payload_hashes,
        ),
    }


def _relay_transfer_entry(
    driver,
    tx: dict,
    contract: str,
    sender: str,
    kwargs: dict,
) -> dict[str, Any] | None:
    inputs = _entry_inputs(driver, contract, "relay_transfer", kwargs)
    if inputs is None:
        return None
    vk_id, commitments, payload_hashes = inputs
    return _command_request(
        contract=contract,
        sender=sender,
        chain_id=tx["b_meta"]["chain_id"],
        kwargs=kwargs,
        vk_id=vk_id,
        commitments=commitments,
        payload_hashes=payload_hashes,
        target_hash=_text_field_hash("shielded-note-relay-transfer"),
        payload_digest=_text_field_hash("transfer"),
        interaction="relay_transfer_shielded",
        domain="shielded-note-relay-v1",
        public_amount=0,
    )


def _execute_command_entry(
    driver,
    tx: dict,
    contract: str,
    sender: str,
    kwargs: dict,
) -> dict[str, Any] | None:
    inputs = _entry_inputs(driver, contract, "command", kwargs)
    if inputs is None:
        return None
    vk_id, commitments, payload_hashes = inputs
    return _command_request(
        contract=contract,
        sender=sender,
        chain_id=tx["b_meta"]["chain_id"],
        kwargs=kwargs,
        vk_id=vk_id,
        commitments=commitments,
        payload_hashes=payload_hashes,
        target_hash=_text_field_hash(kwargs["target_contract"]),
        payload_digest=_command_payload_digest(kwargs.get("payload")),
        interaction="interact",
        domain="shielded-command-v4",
        public_amount=kwargs.get("public_amount", 0),
    )


def build_verification_request(driver, tx: dict) -> dict[str, Any] | None:
    payload = tx.get("payload")
    if not isinstance(payload, dict):
        return None
    kwargs = payload.get("kwargs")
    if not isinstance(kwargs, dict) or "proof_hex" not in kwargs:
        return None
    contract = payload.get("contract")
    function = payload.get("function")
    sender = payload.get("sender")
    if not isinstance(contract, str) or not isinstance(function, str):
        return None
    if not isinstance(sender, str) or sender == "":
        return None

    if function == "deposit_shielded":
        return _deposit_entry(driver, contract, kwargs)
    if function == "transfer_shielded":
        return _transfer_entry(driver, contract, kwargs)
    if function == "relay_transfer_shielded":
        return _relay_transfer_entry(driver, tx, contract, sender, kwargs)
    if function == "withdraw_shielded":
        return _withdraw_entry(driver, contract, kwargs)
    if function == "execute_command":
        return _execute_command_entry(driver, tx, contract, sender, kwargs)
    return None


def _warm_verified_proofs(requests: list[dict]) -> list[bool]:
    # Preverification only warms a cache, so it must never be able to fail a
    # block. Requests come from transaction kwargs; a request the bridge
    # rejects is counted as unverified and execution verifies it normally.
    try:
        return list(zk_bridge.warm_verified_proofs(requests))
    except Exception:
        pass
    results = []
    for request in requests:
        try:
            results.extend(zk_bridge.warm_verified_proofs([request]))
        except Exception:
            results.append(False)
    return results


def warm_shielded_proof_cache(
    *,
    driver,
    txs: list[dict],
) -> ShieldedPreverifyStats:
    if not zk_bridge.is_available():
        return ShieldedPreverifyStats()

    requests = []
    for tx in txs:
        try:
            request = build_verification_request(driver, tx)
        except Exception:
            continue
        if request is not None:
            requests.append(request)

    if not requests:
        return ShieldedPreverifyStats()

    previous_driver = rt.env.get("__Driver")
    rt.env["__Driver"] = driver
    try:
        results = _warm_verified_proofs(requests)
    finally:
        if previous_driver is None:
            rt.env.pop("__Driver", None)
        else:
            rt.env["__Driver"] = previous_driver

    verified_count = sum(1 for result in results if result is True)
    failed_count = len(results) - verified_count
    return ShieldedPreverifyStats(
        candidate_count=len(results),
        verified_count=verified_count,
        failed_count=failed_count,
    )
