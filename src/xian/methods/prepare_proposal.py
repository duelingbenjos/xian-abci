from loguru import logger

from cometbft.abci.v1beta2.types_pb2 import ResponsePrepareProposal
from xian.account_registry import registry_for_app
from xian.app_logging import build_log_fields
from xian.utils.tx import (
    SequentialNonceTracker,
    decode_and_validate_transaction_static_bytes,
    validate_consensus_transaction_after_static,
)


async def prepare_proposal(self, req) -> ResponsePrepareProposal:
    nonce_tracker = SequentialNonceTracker(self.nonce_storage.get_nonce)
    account_registry = registry_for_app(self)
    txs = []
    block_chi_supplied = 0
    block_tx_bytes = 0

    for raw_tx in req.txs:
        next_block_tx_bytes = block_tx_bytes + len(raw_tx)
        if next_block_tx_bytes > req.max_tx_bytes:
            # Keep looking for a smaller transaction without advancing nonce
            # or chi accounting for a transaction that cannot be included.
            continue
        tx = None
        try:
            tx = decode_and_validate_transaction_static_bytes(
                raw_tx,
                chain_id=self.chain_id,
                max_raw_tx_bytes=self.max_tx_bytes,
                account_registry=account_registry,
            )
            self.tx_fee_policy.validate_tx(tx)
            next_block_chi_supplied = block_chi_supplied + self.tx_fee_policy.tx_chi_supplied(tx)
            self.tx_fee_policy.validate_block_total(next_block_chi_supplied)
            validate_consensus_transaction_after_static(
                tx,
                nonce_tracker=nonce_tracker,
            )
            account_registry.accept(tx)
            block_chi_supplied = next_block_chi_supplied
        except Exception as exc:
            logger.bind(
                **build_log_fields(
                    stage="prepare_proposal",
                    tx=tx,
                    raw_tx=raw_tx,
                    block_height=getattr(req, "height", None),
                )
            ).warning(
                "Dropping invalid transaction from proposal preparation: {}",
                exc,
            )
            continue
        txs.append(raw_tx)
        block_tx_bytes = next_block_tx_bytes

    response = ResponsePrepareProposal(txs=txs)
    return response
