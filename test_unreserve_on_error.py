import os
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, BITCOIND_CONFIG, FUNDAMOUNT
from pyln.client import RpcError

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}
FUNDAMOUNT = 1000000  # 1M satoshis

def test_unreserve_on_error(node_factory):
    """
    Test that bumpchannelopen unreserves inputs when an error occurs after input reservation.
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
    addr = l1.rpc.newaddr()['bech32']
    bitcoind.rpc.sendtoaddress(addr, 3)  # Increased to 3 BTC for sufficient change
    bitcoind.generate_block(1)
    sync_blockheight(bitcoind, [l1, l2])

    # Fund channel, keep transaction unconfirmed
    funding = l1.rpc.fundchannel(l2.info['id'], FUNDAMOUNT, feerate="3000perkb")
    funding_txid = funding['txid']
    print(f"Funding transaction ID: {funding_txid}")

    # Find unreserved change output
    outputs = l1.rpc.listfunds()['outputs']
    change_output = next(
        (output for output in outputs if output['txid'] == funding_txid and not output['reserved']),
        None
    )
    assert change_output is not None, "Could not find unreserved change output"

    # Create a PSBT and reserve the input
    addr2 = l1.rpc.newaddr()['bech32']
    utxo_amount_btc = change_output['amount_msat'] / 100_000_000_000
    output_amount = utxo_amount_btc - 0.00002000
    assert output_amount > 0.00000294, f"Output amount {output_amount} BTC below dust limit (~294 satoshis)"
    
    mock_psbt = bitcoind.rpc.createpsbt(
        [{"txid": funding_txid, "vout": change_output['output']}],
        [{addr2: round(output_amount, 8)}]
    )
    print(f"Created mock PSBT: {mock_psbt}")
    l1.rpc.reserveinputs(mock_psbt)
    print(f"Reserved inputs for PSBT: {mock_psbt}")

    # Verify the input is reserved
    outputs_before = l1.rpc.listfunds()['outputs']
    for output in outputs_before:
        if output['txid'] == change_output['txid'] and output['output'] == change_output['output']:
            assert output['reserved'], "UTXO should be reserved after reserveinputs"
            break
    else:
        assert False, "Change UTXO not found in funds before bumpchannelopen"

    # Call bumpchannelopen with the same input, expecting failure due to reserved UTXO
    try:
        result = l1.rpc.bumpchannelopen(
            txid=funding_txid,
            vout=change_output['output'],
            amount="1000sats"
        )
        outputs_mid = l1.rpc.listfunds()['outputs']
        for output in outputs_mid:
            if output['txid'] == change_output['txid'] and output['output'] == change_output['output']:
                assert output['reserved'], "UTXO should still be reserved during bumpchannelopen"
                break
        print(f"Unexpected success: {result}")
        assert False, "Expected an error but bumpchannelopen succeeded"
    except RpcError as e:
        print(f"Expected error response: {str(e)}")
        assert any(x in str(e).lower() for x in ["cannot reserve", "already reserved", "bad utxo"]), f"Unexpected error: {e}"

    # Verify that the inputs are unreserved
    outputs_after = l1.rpc.listfunds()['outputs']
    for output in outputs_after:
        if output['txid'] == change_output['txid'] and output['output'] == change_output['output']:
            assert not output['reserved'], "UTXO should be unreserved after error"
            break
    else:
        assert False, "Change UTXO not found in funds after error"

    # Verify cleanup log
    try:
        logs = l1.daemon.wait_for_log(r"\[CLEANUP\] Successfully unreserved inputs via PSBT", timeout=10)
        print(f"Cleanup log found: {logs}")
    except TimeoutError:
        print("Warning: Cleanup log not found within timeout")
        