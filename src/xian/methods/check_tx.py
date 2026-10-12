import hashlib

from loguru import logger

from cometbft.abci.v1beta3.types_pb2 import ResponseCheckTx
from xian.account_registry import registry_for_app
from xian.app_logging import build_log_fields
from xian.constants import Constants as c
from xian.utils.tx import (
    decode_and_validate_transaction_static_bytes,
    validate_transaction_after_static,
)


def _rejected_check_tx(
    *,
    error: Exception,
    raw_tx: bytes,
    tx: dict | None = None,
) -> ResponseCheckTx:
    log_fields = {"stage": "check_tx", "raw_tx": raw_tx}
    if tx is not None:
        log_fields["tx"] = tx
    logger.bind(**build_log_fields(**log_fields)).warning(
        "Rejected transaction during CheckTx: {}", error
    )
    return ResponseCheckTx(
        code=c.ErrorCode,
        log=f"{type(error).__name__}: {error}",
    )


async def check_tx(self, raw_tx) -> ResponseCheckTx:
    try:
        tx = decode_and_validate_transaction_static_bytes(
            raw_tx,
            chain_id=self.chain_id,
            max_raw_tx_bytes=self.max_tx_bytes,
            account_registry=registry_for_app(self),
        )
    except Exception as e:
        return _rejected_check_tx(error=e, raw_tx=raw_tx)

    try:
        tx_hash = hashlib.sha256(raw_tx).hexdigest()
        raw_driver = self.client.raw_driver
        pending_reads = raw_driver.pending_reads.copy()
        transaction_reads = raw_driver.transaction_reads.copy()
        transaction_read_prefixes = raw_driver.transaction_read_prefixes.copy()
        validate_transaction_after_static(
            self.client,
            self.nonce_storage,
            tx,
            tx_hash=tx_hash,
            fee_policy=self.tx_fee_policy,
        )
        raw_driver.pending_reads = pending_reads
        raw_driver.transaction_reads = transaction_reads
        raw_driver.transaction_read_prefixes = transaction_read_prefixes
        return ResponseCheckTx(code=c.OkCode)
    except Exception as e:
        raw_driver.pending_reads = pending_reads
        raw_driver.transaction_reads = transaction_reads
        raw_driver.transaction_read_prefixes = transaction_read_prefixes
        return _rejected_check_tx(error=e, raw_tx=raw_tx, tx=tx)
