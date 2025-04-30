import os
from pyln.client import RpcError
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, BITCOIND_CONFIG

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}
FUNDAMOUNT = 500000  # Match emergency_reserve for consistency

def test_bumpchannelopen_invalid_utxo(node_factory):
    opts = {
        'bump_brpc_user': BITCOIND_CONFIG["rpcuser"],
        'bump_brpc_pass': BITCOIND_CONFIG["rpcpassword"],
        'bump_brpc_port': BITCOIND_CONFIG["rpcport"]
    }
    opts.update(pluginopt)
    l1, l2 = node_factory.get_nodes(2, opts=opts)

    # Setup with minimal funds
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    bitcoind = l1.bitcoin
    addr = l1.rpc.newaddr()['bech32']
    bitcoind.rpc.sendtoaddress(addr, 0.01)  # 1M sats
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])

    # Fund channel
    funding = l1.rpc.fundchannel(l2.info['id'], FUNDAMOUNT, feerate="1000perkb")
    funding_txid = funding['txid']
    bitcoind.generate_block(1)  # Confirm funding tx
    sync_blockheight(bitcoind, [l1, l2])

    # Test invalid txid
    invalid_txid = "0000000000000000000000000000000000000000000000000000000000000000"
    try:
        l1.rpc.bumpchannelopen(
            txid=invalid_txid,
            vout=0,  # Valid vout index, but txid is invalid
            fee_rate=3
        )
        assert False, "Expected bump to fail with invalid txid"
    except RpcError as e:
        print(f"Expected error (invalid txid): {e}")
        assert "not found" in str(e).lower() or "invalid" in str(e).lower(), f"Expected invalid txid error, got {e}"

    # Test invalid vout
    try:
        l1.rpc.bumpchannelopen(
            txid=funding_txid,
            vout=999,  # Invalid vout
            fee_rate=3
        )
        assert False, "Expected bump to fail with invalid vout"
    except RpcError as e:
        print(f"Expected error (invalid vout): {e}")
        assert "not found" in str(e).lower() or "invalid" in str(e).lower(), f"Expected invalid vout error, got {e}"
        