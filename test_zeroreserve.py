import os
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, BITCOIND_CONFIG

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}
FUNDAMOUNT = 500000

def test_bumpchannelopen_zero_reserve(node_factory):
    opts = {
        'bump_brpc_user': BITCOIND_CONFIG["rpcuser"],
        'bump_brpc_pass': BITCOIND_CONFIG["rpcpassword"],
        'bump_brpc_port': BITCOIND_CONFIG["rpcport"]
    }
    opts.update(pluginopt)
    l1, l2 = node_factory.get_nodes(2, opts=opts)

    # Setup with large funds to ensure zero reserve
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    bitcoind = l1.bitcoin
    addr = l1.rpc.newaddr()['bech32']
    bitcoind.rpc.sendtoaddress(addr, 0.1)  # 10M sats
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])

    # Fund channel
    funding = l1.rpc.fundchannel(l2.info['id'], FUNDAMOUNT, feerate="1000perkb")
    funding_txid = funding['txid']
    bitcoind.generate_block(1)  # Confirm funding tx
    sync_blockheight(bitcoind, [l1, l2])

    # Find change output
    outputs = l1.rpc.listfunds()['outputs']
    change_output = next(o for o in outputs if o['txid'] == funding_txid and not o['reserved'])

    # Bump channel open with higher feerate
    target_feerate = 10  # Higher feerate for distinct response
    new_address = l1.rpc.newaddr()['bech32']
    try:
        result = l1.rpc.bumpchannelopen(
            txid=funding_txid,
            vout=change_output['output'],
            fee_rate=target_feerate,
            address=new_address
        )
    except Exception as e:
        assert False, f"Bumpchannelopen failed: {e}"

    # Log result for debugging
    print(f"bump_result: {result}")

    # Verify zero reserve logic
    total_sats = sum(o['amount_msat'] // 1000 for o in outputs if not o['reserved'])
    expected_reserve = max(25000 - total_sats, 0)
    print(f"Total sats: {total_sats}, Expected reserve: {expected_reserve}, Child fee: {result['child_fee']}")

    # Check child fee
    assert result['child_fee'] > 0, "Child fee should be positive"

    # Check total fees and zero reserve
    assert expected_reserve == 0, f"Expected zero reserve, got {expected_reserve}"
    expected_total_fees = result['child_fee']  # No reserve
    assert abs(result['total_fees'] - expected_total_fees) < 200, f"Expected total fees ~{expected_total_fees}, got {result['total_fees']}"
    print(f"Expected total fees: {expected_total_fees}, Actual total fees: {result['total_fees']}")
    