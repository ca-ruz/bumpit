import os
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, FUNDAMOUNT, BITCOIND_CONFIG

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpchannelopen.py")}
FUNDAMOUNT = 500000

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

def test_bumpchannelopen_emergency_reserve(node_factory):
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

    # Find change output
    outputs = l1.rpc.listfunds()['outputs']
    change_output = next(o for o in outputs if o['txid'] == funding_txid and not o['reserved'])

    # Bump with emergency reserve needed
    target_feerate = 3
    result = l1.rpc.bumpchannelopen(
        txid=funding_txid,
        vout=change_output['output'],
        fee_rate=target_feerate,
        address=l1.rpc.newaddr()['bech32']
    )

    # Verify emergency reserve logic
    total_sats = sum(o['amount_msat'] // 1000 for o in outputs if not o['reserved'])
    expected_reserve = max(25000 - total_sats, 0)
    assert result['third_child_fee_sat'] > 0, "Child fee should be positive"
    assert result['total_fees'] >= expected_reserve + result['third_child_fee_sat'], "Total fees should account for reserve"
    