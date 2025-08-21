import os
from pyln.client import RpcError
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, BITCOIND_CONFIG

# import debugpy
# debugpy.listen(("localhost", 5678))

import pytest

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}
FUNDAMOUNT = 50000  # Channel funding amount in satoshis
INITIAL_FUNDING = 200000  # Initial wallet funding in satoshis

def test_confirmed_bump(node_factory):
    opts = {
        'bump_brpc_user': BITCOIND_CONFIG["rpcuser"],
        'bump_brpc_pass': BITCOIND_CONFIG["rpcpassword"],
        'bump_brpc_port': BITCOIND_CONFIG["rpcport"]
    }
    opts.update(pluginopt)
    l1, l2 = node_factory.get_nodes(2, opts=opts)

    # Step 1: Fund l1's on-chain wallet with 100,000 sats
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    bitcoind = l1.bitcoin
    addr = l1.rpc.newaddr()['bech32']
    bitcoind.rpc.sendtoaddress(addr, INITIAL_FUNDING / 1e8)  # 100,000 sats in BTC
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])

    # Verify wallet balance
    funds = l1.rpc.listfunds()
    outputs = funds.get("outputs", [])
    assert len(outputs) == 1, f"Expected 1 UTXO, got {len(outputs)}"
    assert outputs[0]["amount_msat"] == INITIAL_FUNDING * 1000, f"Expected {INITIAL_FUNDING} sats, got {outputs[0]['amount_msat'] / 1000} sats"

    # Step 2: Open a channel with 74,000 sats and confirm it
    funding = l1.rpc.fundchannel(l2.info['id'], FUNDAMOUNT, feerate="250perkb")
    funding_txid = funding['txid']
    bitcoind.generate_block(1)  # Confirm funding tx
    sync_blockheight(bitcoind, [l1, l2])

    # Verify funding transaction is confirmed
    funds = l1.rpc.listfunds()
    outputs = funds.get("outputs", [])
    funding_utxo = None

    # for txid in outputs:
    #     if txid["txid"] != funding_txid:
    #         confirmed_txid = txid

    for output in outputs:
        if output["txid"] == funding_txid:
            funding_utxo = output
            break
    assert funding_utxo is not None and funding_utxo.get("status") == "confirmed", f"Funding tx {funding_txid} is not confirmed"

    # Step 3: Attempt to bump the confirmed funding transaction
    with pytest.raises(RpcError) as exc_info:
        l1.rpc.bumpchannelopen(
            txid=funding_txid,  # Use funding_txid instead of wallet_txid
            vout=funding_utxo["output"],  # Use funding_utxo's vout
            amount="3satvb"
        )

    # Step 4: Assert the outcome
    assert exc_info.type is RpcError
    assert exc_info.value.error["message"] == "Error while processing bumpchannelopen: Transaction is already confirmed and cannot be bumped"
    print(f"Success: Cannot bump confirmed transaction: {exc_info.value.error["message"]}")
