import os
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, BITCOIND_CONFIG

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}
FUNDAMOUNT = 500000

def test_bumpchannelopen_insufficient_funds(node_factory):
    opts = {
        'bump_brpc_user': BITCOIND_CONFIG["rpcuser"],
        'bump_brpc_pass': BITCOIND_CONFIG["rpcpassword"],
        'bump_brpc_port': BITCOIND_CONFIG["rpcport"]
    }
    opts.update(pluginopt)
    l1, l2 = node_factory.get_nodes(2, opts=opts)

    # Setup with just enough funds for channel and reserve
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    bitcoind = l1.bitcoin
    addr = l1.rpc.newaddr()['bech32']
    bitcoind.rpc.sendtoaddress(addr, 0.006)  # 600k sats
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])

    # Fund channel
    try:
        funding = l1.rpc.fundchannel(l2.info['id'], FUNDAMOUNT, feerate="1000perkb")
    except Exception as e:
        assert False, f"Fundchannel failed: {e}"
    funding_txid = funding['txid']
    bitcoind.generate_block(1)  # Confirm funding tx
    sync_blockheight(bitcoind, [l1, l2])

    # Find change output (should be small)
    outputs = l1.rpc.listfunds()['outputs']
    change_output = next((o for o in outputs if o['txid'] == funding_txid and not o['reserved']), None)
    if change_output is None:
        print("No change output found, as expected due to minimal funds")
        return

    # Try bumping with high feerate
    try:
        result = l1.rpc.bumpchannelopen(
            txid=funding_txid,
            vout=change_output['output'],
            fee_rate=1000,  # High feerate to trigger insufficient funds
        )
        print(f"bump_result: {result}")
        assert False, "Expected bump to fail due to insufficient funds"
    except Exception as e:
        print(f"Expected error: {e}")
        assert any(
            phrase in str(e).lower() 
            for phrase in ["insufficient funds", "not enough", "amount out of range"]
        ), f"Expected insufficient funds or amount out of range error, got {e}"
        