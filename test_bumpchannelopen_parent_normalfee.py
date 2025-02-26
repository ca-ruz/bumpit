import os
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, FUNDAMOUNT, BITCOIND_CONFIG

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpchannelopen.py")}

FUNDAMOUNT = 1000000  # Match the manual test amount of 1M sats

def test_bumpchannelopen(node_factory):
    # Basic setup
    opts = {
        'bump_brpc_user': BITCOIND_CONFIG["rpcuser"],
        'bump_brpc_pass': BITCOIND_CONFIG["rpcpassword"],
        'bump_brpc_port': BITCOIND_CONFIG["rpcport"]
    }
    opts.update(pluginopt)
    l1, l2 = node_factory.get_nodes(2, opts=opts)
    
    # Connect nodes and create channel
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    bitcoind = l1.bitcoin
    addr = l1.rpc.newaddr()['bech32']
    bitcoind.rpc.sendtoaddress(addr, 1)
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])
    
    # Create the funding transaction
    funding = l1.rpc.fundchannel(l2.info['id'], FUNDAMOUNT, 10000)
    funding_txid = funding['txid']
    print(f"Funding tx id after funding channel: {funding_txid}")

    funding_txid_details = bitcoind.rpc.getrawtransaction(funding_txid)
    # funding_txid_details = bitcoind.rpc.decoderawtransaction(funding_txid)
    print(f"Funding txid details: {funding_txid_details}")
    
    # Find the change output using listfunds
    outputs = l1.rpc.listfunds()['outputs']
    change_output = None
    for output in outputs:
        if output['txid'] == funding_txid and not output['reserved']:
            change_output = output
            break
    
    assert change_output is not None, "Could not find unreserved change output"

    # Call bumpchannelopen
    target_feerate = 10
    result = l1.rpc.bumpchannelopen(
        txid=funding_txid,
        vout=change_output['output'],
        fee_rate=target_feerate,
        address=l1.rpc.newaddr()['bech32']
    )

    # Debug: print the result to see what keys are available
    print("Result keys:", result.keys())

    # Extract fees and sizes from the plugin result
    parent_fee_sats = result.get('parent_fee', 0)
    child_fee_sats = result.get('child_fee', 0)
    total_fee_sats = result.get('total_fees', 0)
    parent_vsize = result.get('parent_vsize', 0)
    child_vsize = result.get('child_vsize', 0)
    total_vsize = result.get('total_vsizes', 0)

    # Print debug info
    print(f"Parent transaction details: txid={result['parent_txid']}, fee={parent_fee_sats}, vsize={parent_vsize}")
    print(f"Child transaction details: txid={result['child_txid']}, fee={child_fee_sats}, vsize={child_vsize}")
    print(f"Package details: total fees={total_fee_sats}, total vsizes={total_vsize}")

    # Assertions
    assert child_fee_sats > 0, "Child fee should be positive"
    total_feerate = total_fee_sats / total_vsize if total_vsize > 0 else 0
    assert total_feerate == target_feerate, f"Total feerate should be {target_feerate}, got {total_feerate}"

    # Additional debugging output as needed
