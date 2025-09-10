import os
from pyln.client import RpcError
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, BITCOIND_CONFIG
import pytest

# import debugpy
# debugpy.listen(("localhost", 5678))

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}

def test_unreserve_on_failure(node_factory):
    """
    Test that bumpchannelopen unreserves inputs when a failure occurs after input reservation (e.g., dust error on broadcast in yolo mode).
    """
    # Set up nodes with plugin options
    opts = {
        'bump_brpc_user': BITCOIND_CONFIG["rpcuser"],
        'bump_brpc_pass': BITCOIND_CONFIG["rpcpassword"],
        'bump_brpc_port': BITCOIND_CONFIG["rpcport"]
    }
    opts.update(pluginopt)
    l1, l2 = node_factory.get_nodes(2, opts=opts)

    # Connect nodes and fund l1 with two transactions
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    bitcoind = l1.bitcoin
    addr = l1.rpc.newaddr()['bech32']
    bitcoind.rpc.sendtoaddress(addr, 0.002)  # 200,000 satoshis
    bitcoind.rpc.sendtoaddress(addr, 0.001)  # 100,000 satoshis for reserve
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])

    # Fund channel, keep transaction unconfirmed
    funding = l1.rpc.fundchannel(l2.info['id'], 100000, feerate="3000perkb")  # 100,000 satoshis
    funding_txid = funding['txid']
    print(f"Funding transaction ID: {funding_txid}")

    # Find unreserved change output
    outputs = l1.rpc.listfunds()['outputs']
    change_output = next(
        (output for output in outputs if output['txid'] == funding_txid and not output['reserved']),
        None
    )
    assert change_output is not None, "Could not find unreserved change output"

    # Verify the input starts unreserved
    outputs_before = l1.rpc.listfunds()['outputs']
    for output in outputs_before:
        if output['txid'] == change_output['txid'] and output['output'] == change_output['output']:
            assert not output['reserved'], "UTXO should start unreserved"
            break
    else:
        assert False, "Change UTXO not found in funds before bumpchannelopen"

    # Calculate fee to leave 293 satoshis (dust)
    utxo_amount_sat = change_output['amount_msat'] // 1000
    high_fee_sat = utxo_amount_sat - 293  # Leave 293 sat (below dust)
    amount = f"{high_fee_sat}sats"
    print(f"Using amount to trigger dust error: {amount}")

    # Call bumpchannelopen with yolo mode, expecting failure after reservation (dust on broadcast)
    with pytest.raises(RpcError) as exc_info:
        result = l1.rpc.bumpchannelopen(
            txid=funding_txid,
            vout=change_output['output'],
            amount=amount,
            yolo="yolo"
        )
        print(f"Unexpected success: {result}")
        assert False, "Expected an error but bumpchannelopen succeeded"
    print(f"Error from bumpchannelopen: {exc_info.value.error['message']}")
    error_msg = exc_info.value.error["message"].lower()
    assert "dust" in error_msg, f"Unexpected error (expected dust-related failure): {error_msg}"

    # Verify that the inputs are unreserved after the error
    outputs_after = l1.rpc.listfunds()['outputs']
    for output in outputs_after:
        if output['txid'] == change_output['txid'] and output['output'] == change_output['output']:
            print(f"UTXO reserved status after error: {output['reserved']}")
            assert not output['reserved'], "UTXO should be unreserved after plugin failure"
            break
    else:
        assert False, "Change UTXO not found in funds after error"
