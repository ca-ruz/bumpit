import os
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, FUNDAMOUNT, BITCOIND_CONFIG

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpchannelopen.py")}
FUNDAMOUNT = 1000000  # Match the manual test amount of 1M sats

def calculate_parent_tx_details(bitcoind, txid):
    """
    Calculate fee, feerate, and vsize for a parent transaction given its txid.
    
    Args:
        bitcoind: Bitcoin RPC connection
        txid: Transaction ID of the parent transaction
    
    Returns:
        dict: Contains fee (in satoshis), feerate (sat/vB), and vsize (vbytes)
    """
    # Get raw transaction details
    tx_hex = bitcoind.rpc.getrawtransaction(txid)
    tx_details = bitcoind.rpc.decoderawtransaction(tx_hex)
    
    # Calculate total inputs (in BTC)
    total_inputs = 0
    for vin in tx_details["vin"]:
        input_tx = bitcoind.rpc.getrawtransaction(vin["txid"], True)
        total_inputs += input_tx["vout"][vin["vout"]]["value"]
    
    # Calculate total outputs (in BTC)
    total_outputs = sum(vout["value"] for vout in tx_details["vout"])
    
    # Calculate fee in satoshis
    fee_btc = total_inputs - total_outputs
    fee_sats = int(fee_btc * 10**8)
    
    # Get transaction vsize
    vsize = tx_details["vsize"]
    
    # Calculate feerate (sat/vB)
    feerate = fee_sats / vsize if vsize > 0 else 0
    
    return {
        "fee": fee_sats,
        "vsize": vsize,
        "feerate": feerate
    }

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
    funding = l1.rpc.fundchannel(l2.info['id'], FUNDAMOUNT, 3000)
    funding_txid = funding['txid']
    print(f"Funding tx id after funding channel: {funding_txid}")

    # Find the change output using listfunds
    outputs = l1.rpc.listfunds()['outputs']
    change_output = None
    for output in outputs:
        if output['txid'] == funding_txid and not output['reserved']:
            change_output = output
            break
    
    assert change_output is not None, "Could not find unreserved change output"

    # Calculate parent transaction details independently
    parent_details = calculate_parent_tx_details(bitcoind, funding_txid)
    print(f"Calculated parent details: fee={parent_details['fee']} sats, "
        f"vsize={parent_details['vsize']} vB, feerate={parent_details['feerate']} sat/vB")

    # Call bumpchannelopen
    target_feerate = 3 # Desired feerate in sat/vB
    result = l1.rpc.bumpchannelopen(
        txid=funding_txid,
        vout=change_output['output'],
        fee_rate=target_feerate,
        address=l1.rpc.newaddr()['bech32']
    )

    # Extract values from plugin result
    plugin_parent_fee = result.get('parent_fee', 0)
    plugin_parent_vsize = result.get('parent_vsize', 0)
    plugin_parent_feerate = result.get('parent_feerate', 0)
    plugin_child_fee = result.get('third_child_fee_sat"', 0)
    plugin_child_vsize = result.get('child_vsize', 0)
    plugin_child_feerate = result.get('child_feerate', 0)
    plugin_total_fees = result.get('total_fees', 0)
    plugin_total_vsizes = result.get('total_vsizes', 0)
    plugin_total_feerate = result.get('total_feerate', 0)

    # Print plugin results for debugging
    print(f"Plugin parent details: fee={plugin_parent_fee} sats, "
          f"vsize={plugin_parent_vsize} vB, feerate={plugin_parent_feerate} sat/vB")
    print(f"Plugin child details: fee={plugin_child_fee} sats, "
          f"vsize={plugin_child_vsize} vB, feerate={plugin_child_feerate} sat/vB")
    print(f"Plugin total: fees={plugin_total_fees} sats, vsizes={plugin_total_vsizes} vB, "
          f"feerate={plugin_total_feerate} sat/vB")

    # Compare calculated parent details with plugin output
    assert plugin_parent_fee == parent_details['fee'], (
        f"Parent fee mismatch: plugin={plugin_parent_fee}, calculated={parent_details['fee']}"
    )
    assert plugin_parent_vsize == parent_details['vsize'], (
        f"Parent vsize mismatch: plugin={plugin_parent_vsize}, calculated={parent_details['vsize']}"
    )
    assert abs(plugin_parent_feerate - parent_details['feerate']) < 0.01, (
        f"Parent feerate mismatch: plugin={plugin_parent_feerate}, calculated={parent_details['feerate']}"
    )

    # Verify child fee is positive
    assert plugin_child_fee > 0, "Child fee should be positive"

    # Recalculate total feerate and compare with target
    calculated_total_feerate = plugin_total_fees / plugin_total_vsizes if plugin_total_vsizes > 0 else 0
    print(f"Recalculated total feerate: {calculated_total_feerate} sat/vB")
    
    # Allow for small floating-point differences
    assert abs(calculated_total_feerate - target_feerate) < 0.1, (
        f"Total feerate doesn't match target: target={target_feerate}, "
        f"calculated={calculated_total_feerate}"
    )
    
    # Verify plugin-reported total_feerate matches calculation
    assert abs(plugin_total_feerate - calculated_total_feerate) < 0.01, (
        f"Plugin total feerate doesn't match calculation: "
        f"plugin={plugin_total_feerate}, calculated={calculated_total_feerate}"
    )

if __name__ == "__main__":
    # For manual testing
    from pyln.testing.fixtures import setup_node_factory
    node_factory = setup_node_factory()
    test_bumpchannelopen(node_factory)
    