import os
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, FUNDAMOUNT, BITCOIND_CONFIG

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}
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
    tx_hex = bitcoind.rpc.getrawtransaction(txid)
    tx_details = bitcoind.rpc.decoderawtransaction(tx_hex)
    
    total_inputs = sum(
        bitcoind.rpc.getrawtransaction(vin["txid"], True)["vout"][vin["vout"]]["value"]
        for vin in tx_details["vin"]
    )
    
    total_outputs = sum(vout["value"] for vout in tx_details["vout"])
    
    fee_sats = int((total_inputs - total_outputs) * 10**8)
    vsize = tx_details["vsize"]
    feerate = fee_sats / vsize if vsize > 0 else 0
    
    return {
        "fee": fee_sats,
        "vsize": vsize,
        "feerate": feerate
    }

def test_child_highfee(node_factory):
    """
    Test bumpchannelopen with a high target feerate (1000 sat/vB) to ensure it handles
    large child fees correctly without crashing.
    """
    # Set up nodes with plugin options
    opts = {
        'bump_brpc_user': BITCOIND_CONFIG["rpcuser"],
        'bump_brpc_pass': BITCOIND_CONFIG["rpcpassword"],
        'bump_brpc_port': BITCOIND_CONFIG["rpcport"]
    }
    opts.update(pluginopt)
    l1, l2 = node_factory.get_nodes(2, opts=opts)
    
    # Connect nodes and fund a channel
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    bitcoind = l1.bitcoin
    addr = l1.rpc.newaddr()['bech32']
    bitcoind.rpc.sendtoaddress(addr, 1)
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])
    
    funding = l1.rpc.fundchannel(l2.info['id'], FUNDAMOUNT, 3000)
    funding_txid = funding['txid']
    print(f"Funding transaction ID: {funding_txid}")
    
    # Find unreserved change output
    outputs = l1.rpc.listfunds()['outputs']
    change_output = next(
        (output for output in outputs if output['txid'] == funding_txid and not output['reserved']),
        None
    )
    assert change_output is not None, "Could not find unreserved change output"
    
    # Calculate parent details
    parent_details = calculate_parent_tx_details(bitcoind, funding_txid)
    print(f"Parent transaction details:")
    print(f"  Fee: {parent_details['fee']} sats")
    print(f"  Vsize: {parent_details['vsize']} vB")
    print(f"  Feerate: {parent_details['feerate']:.2f} sat/vB")
    
    # Call bumpchannelopen with high feerate
    target_feerate = 1000 # sat/vB
    result = l1.rpc.bumpchannelopen(
        txid=funding_txid,
        vout=change_output['output'],
        fee_rate=target_feerate,
        yolo="dryrun"
    )
    
    # Extract plugin results
    plugin_parent_fee = result.get('parent_fee', 0)
    plugin_parent_vsize = result.get('parent_vsize', 0)
    plugin_parent_feerate = result.get('parent_feerate', 0)
    plugin_child_fee = result.get('child_fee', 0)
    plugin_child_vsize = result.get('child_vsize', 0)
    plugin_child_feerate = result.get('child_feerate', 0)
    plugin_total_fees = result.get('total_fees', 0)
    plugin_total_vsizes = result.get('total_vsizes', 0)
    plugin_total_feerate = result.get('total_feerate', 0)
    
    # Print plugin output
    print("\nPlugin response:")
    print(f"  Message: {result.get('message', 'N/A')}")
    print(f"  Analyze command: {result.get('analyze_command', 'N/A')}")
    print(f"  Parent details:")
    print(f"    Fee: {plugin_parent_fee} sats")
    print(f"    Vsize: {plugin_parent_vsize} vB")
    print(f"    Feerate: {plugin_parent_feerate:.2f} sat/vB")
    print(f"  Child details:")
    print(f"    Fee: {plugin_child_fee} sats")
    print(f"    Vsize: {plugin_child_vsize} vB")
    print(f"    Feerate: {plugin_child_feerate:.2f} sat/vB")
    print(f"  Total details:")
    print(f"    Fees: {plugin_total_fees} sats")
    print(f"    Vsizes: {plugin_total_vsizes} vB")
    print(f"    Feerate: {plugin_total_feerate:.2f} sat/vB")
    
    # Verify parent details
    assert plugin_parent_fee == parent_details['fee'], (
        f"Parent fee mismatch: plugin={plugin_parent_fee}, calculated={parent_details['fee']}"
    )
    assert plugin_parent_vsize == parent_details['vsize'], (
        f"Parent vsize mismatch: plugin={plugin_parent_vsize}, calculated={parent_details['vsize']}"
    )
    assert abs(plugin_parent_feerate - parent_details['feerate']) < 0.01, (
        f"Parent feerate mismatch: plugin={plugin_parent_feerate:.2f}, calculated={parent_details['feerate']:.2f}"
    )
    
    # Verify child fee is positive and reasonably large
    assert plugin_child_fee > 10000, "Child fee must be significantly positive for high feerate"
    
    # Verify total feerate is positive and reasonable
    calculated_total_feerate = plugin_total_fees / plugin_total_vsizes if plugin_total_vsizes > 0 else 0
    print(f"Recalculated total feerate: {calculated_total_feerate:.2f} sat/vB")
    
    assert calculated_total_feerate > 0, "Total feerate must be positive"
    assert abs(plugin_total_feerate - calculated_total_feerate) < 0.01, (
        f"Plugin total feerate mismatch: plugin={plugin_total_feerate:.2f}, calculated={calculated_total_feerate:.2f}"
    )
