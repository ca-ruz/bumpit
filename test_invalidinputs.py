import os
from pyln.client import RpcError
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, BITCOIND_CONFIG

# import debugpy
# debugpy.listen(("localhost", 5678))

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}
FUNDAMOUNT = 500000  # Match emergency_reserve for consistency

def test_invalidinputs(node_factory):
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

    vout = 0

    # Test invalid txid
    with pytest.raises(RpcError) as exc_info:
        invalid_txid = "0000000000000000000000000000000000000000000000000000000000000000"
        l1.rpc.bumpchannelopen(
            txid=invalid_txid,
            vout=vout,  # Valid vout index, but txid is invalid
            amount="3satvb"
        )

    # assert "code" in result and result["code"] == -32600, f"Expected error code -32600, got {result}"
    # assert "message" in result, f"Expected error message, got {result}"
    # print(f"Expected error (invalid txid): {result['message']}")
    # assert "not found" in result["message"].lower() or "invalid" in result["message"].lower(), f"Expected invalid txid error, got {result['message']}"

    # Step 4: Assert the outcome
    assert exc_info.type is RpcError
    assert exc_info.value.error["message"] == f"Error while processing bumpchannelopen: UTXO {invalid_txid}:{vout} not found in available UTXOs"
    # print(f"Success: Cannot bump confirmed transaction: {exc_info.value.error["message"]}")

    vout2 = 999

    # Test invalid vout
    with pytest.raises(RpcError) as exc_info:
        l1.rpc.bumpchannelopen(
            txid=funding_txid,
            vout=vout2,  # Invalid vout
            amount="3satvb"
        )

    # assert "code" in result and result["code"] == -32600, f"Expected error code -32600, got {result}"
    # assert "message" in result, f"Expected error message, got {result}"
    # print(f"Expected error (invalid vout): {result['message']}")
    # assert "not found" in result["message"].lower() or "invalid" in result["message"].lower(), f"Expected invalid vout error, got {result['message']}"

    
    # Step 4: Assert the outcome
    assert exc_info.type is RpcError
    assert exc_info.value.error["message"] == f"Error while processing bumpchannelopen: UTXO {funding_txid}:{vout2} not found in available UTXOs"
    # print(f"Success: Cannot bump confirmed transaction: {exc_info.value.error["message"]}")
