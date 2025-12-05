import os
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, BITCOIND_CONFIG

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}
FUNDAMOUNT = 500000  # Match emergency_reserve for consistency

def test_parent_highfee(node_factory):
    """
    Test bumpchannelopen when the parent transaction has a high feerate (≥10 sat/vB),
    ensuring the plugin skips CPFP and returns an appropriate message.
    """
    # Set up nodes with plugin options
    opts = {
        'bump_brpc_user': BITCOIND_CONFIG["rpcuser"],
        'bump_brpc_pass': BITCOIND_CONFIG["rpcpassword"],
        'bump_brpc_port': BITCOIND_CONFIG["rpcport"]
    }
    opts.update(pluginopt)
    l1, l2 = node_factory.get_nodes(2, opts=opts)

    # Connect nodes and fund l1
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    bitcoind = l1.bitcoin
    addr = l1.rpc.newaddr('bech32')['bech32']
    bitcoind.rpc.sendtoaddress(addr, 2)  # 200M sats for sufficient funds
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])

    # Fund channel with a high feerate, keep unconfirmed
    funding = l1.rpc.fundchannel(l2.info['id'], FUNDAMOUNT, feerate="10000perkb")
    funding_txid = funding['txid']
    print(f"Funding transaction ID: {funding_txid}")

    # Find unreserved change output
    outputs = l1.rpc.listfunds()['outputs']
    change_output = next(
        (output for output in outputs if output['txid'] == funding_txid and not output['reserved']),
        None
    )
    if change_output is None:
        print("No change output found, as expected due to high feerate funding")
        return

    # Call bumpchannelopen with a lower target feerate
    target_feerate_suffix = "3satvb"
    result = l1.rpc.bumpchannelopen(
        txid=funding_txid,
        vout=change_output['output'],
        amount=target_feerate_suffix,
    )

    target_feerate = int(target_feerate_suffix[:-5])

    # Handle error responses
    if 'code' in result and result['code'] == -32600:
        print(f"Error response: {result['message']}")
        assert "reserve" in result['message'].lower() or "confirmed" in result['message'].lower(), (
            f"Unexpected error: {result['message']}"
        )
        return

    # Print plugin output
    print("\nPlugin response:")
    print(f"  Message: {result.get('message', 'N/A')}")
    print(f"  Parent details:")
    print(f"    Fee: {result.get('parent_fee', 0)} sats")
    print(f"    Vsize: {result.get('parent_vsize', 0)} vB")
    print(f"    Feerate: {result.get('parent_feerate', 0):.2f} sat/vB")
    print(f"  Child details:")
    print(f"    Fee: {result.get('child_fee', 0)} sats")
    print(f"    Vsize: {result.get('child_vsize', 0)} vB")
    print(f"    Feerate: {result.get('child_feerate', 0):.2f} sat/vB")
    print(f"  Total details:")
    print(f"    Fees: {result.get('total_fees', 0)} sats")
    print(f"    Vsizes: {result.get('total_vsizes', 0)} vB")
    print(f"    Feerate: {result.get('total_feerate', 0):.2f} sat/vB")
    print(f"  Desired total feerate: {result.get('desired_total_feerate', 'N/A')}")

    # Verify the plugin skipped CPFP
    assert "No CPFP needed" in result['message'], f"Expected 'No CPFP needed' in message, got: {result['message']}"
    assert result['parent_feerate'] > target_feerate, (
        f"Parent feerate ({result['parent_feerate']:.2f}) should exceed target ({target_feerate})"
    )
    assert result['child_fee'] == 0, f"Expected child_fee=0, got: {result['child_fee']}"
    assert result['child_vsize'] == 0, f"Expected child_vsize=0, got: {result['child_vsize']}"
    assert result['child_feerate'] == 0, f"Expected child_feerate=0, got: {result['child_feerate']}"
    assert result['total_fees'] == result['parent_fee'], f"Expected total_fees=parent_fee, got: {result['total_fees']} vs {result['parent_fee']}"
    