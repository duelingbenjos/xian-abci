from __future__ import annotations

from dataclasses import dataclass

from xian.exceptions import TransactionException

PAID_METERED = "paid_metered"
FREE_METERED = "free_metered"
SYSTEM_UNMETERED = "system_unmetered"

DEFAULT_TX_FEE_MODE = PAID_METERED
DEFAULT_FREE_TX_MAX_CHI = 1_000_000
DEFAULT_FREE_BLOCK_MAX_CHI = 20_000_000

SUPPORTED_TX_FEE_MODES = frozenset({PAID_METERED, FREE_METERED})


def normalize_tx_fee_mode(mode: object | None) -> str:
    normalized = str(mode or DEFAULT_TX_FEE_MODE).strip().lower()
    if normalized not in SUPPORTED_TX_FEE_MODES:
        raise ValueError(f"tx_fee_mode must be one of {sorted(SUPPORTED_TX_FEE_MODES)}")
    return normalized


def _positive_int(value: object, *, name: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be greater than zero") from exc
    if normalized <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return normalized


@dataclass(frozen=True, slots=True)
class TxFeePolicy:
    mode: str
    meter_execution: bool
    charge_fees: bool
    distribute_fee_rewards: bool
    require_chi_balance: bool
    max_tx_chi: int | None = None
    max_block_chi: int | None = None

    @classmethod
    def from_runtime_config(cls, config: dict[str, object] | None) -> "TxFeePolicy":
        payload = config or {}
        mode = normalize_tx_fee_mode(payload.get("tx_fee_mode"))
        if mode == PAID_METERED:
            return cls.paid_metered()

        return cls.free_metered(
            max_tx_chi=_positive_int(
                payload.get("free_tx_max_chi", DEFAULT_FREE_TX_MAX_CHI),
                name="free_tx_max_chi",
            ),
            max_block_chi=_positive_int(
                payload.get("free_block_max_chi", DEFAULT_FREE_BLOCK_MAX_CHI),
                name="free_block_max_chi",
            ),
        )

    @classmethod
    def paid_metered(
        cls,
        *,
        max_tx_chi: int | None = None,
        max_block_chi: int | None = None,
    ) -> "TxFeePolicy":
        return cls(
            mode=PAID_METERED,
            meter_execution=True,
            charge_fees=True,
            distribute_fee_rewards=True,
            require_chi_balance=True,
            max_tx_chi=max_tx_chi,
            max_block_chi=max_block_chi,
        )

    @classmethod
    def free_metered(
        cls,
        *,
        max_tx_chi: int = DEFAULT_FREE_TX_MAX_CHI,
        max_block_chi: int = DEFAULT_FREE_BLOCK_MAX_CHI,
    ) -> "TxFeePolicy":
        return cls(
            mode=FREE_METERED,
            meter_execution=True,
            charge_fees=False,
            distribute_fee_rewards=False,
            require_chi_balance=False,
            max_tx_chi=max_tx_chi,
            max_block_chi=max_block_chi,
        )

    @classmethod
    def system_unmetered(cls) -> "TxFeePolicy":
        return cls(
            mode=SYSTEM_UNMETERED,
            meter_execution=False,
            charge_fees=False,
            distribute_fee_rewards=False,
            require_chi_balance=False,
        )

    def tx_chi_supplied(self, tx: dict) -> int:
        payload = tx.get("payload") if isinstance(tx, dict) else None
        if not isinstance(payload, dict):
            raise TransactionException("Transaction payload is missing")
        try:
            chi_supplied = int(payload.get("chi_supplied") or 0)
        except (TypeError, ValueError) as exc:
            raise TransactionException("Transaction chi_supplied is invalid") from exc
        if chi_supplied < 0:
            raise TransactionException("Transaction chi_supplied is invalid")
        return chi_supplied

    def validate_tx(self, tx: dict) -> None:
        chi_supplied = self.tx_chi_supplied(tx)
        # The VM treats a zero chi budget as "no limit", so a metered
        # transaction must always carry a positive budget.
        if self.meter_execution and chi_supplied <= 0:
            raise TransactionException("Transaction chi_supplied must be greater than zero")
        if self.max_tx_chi is None:
            return
        if chi_supplied > self.max_tx_chi:
            raise TransactionException(
                f"Transaction chi_supplied exceeds maximum configured limit of {self.max_tx_chi}"
            )

    def validate_block_total(self, total_chi_supplied: int) -> None:
        if self.max_block_chi is None:
            return
        if total_chi_supplied > self.max_block_chi:
            raise TransactionException(
                f"Block chi_supplied exceeds maximum configured limit of {self.max_block_chi}"
            )
