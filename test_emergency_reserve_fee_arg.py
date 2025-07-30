import os
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, BITCOIND_CONFIG

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}
FUNDAMOUNT = 74000
INITIAL_FUNDING = 100000
EMERGENCY_RESERVE = 25000

def test_emergency_reserve_fee_boundary(node_factory):
    opts = {
        'bump_brpc_user': BITCOIND_CONFIG["rpcuser"],
        'bump_brpc_pass': BITCOIND_CONFIG["rpcpassword"],
        'bump_brpc_port': BITCOIND_CONFIG["rpcport"]
    }
    opts.update(pluginopt)
    l1, l2 = node_factory.get_nodes(2, opts=opts)

    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    addr = l1.rpc.newaddr()['bech32']
    l1.bitcoin.rpc.sendtoaddress(addr, INITIAL_FUNDING / 1e8)
    l1.bitcoin.generate_block(1)
    sync_blockheight(l1.bitcoin, [l1, l2])

    funding = l1.rpc.fundchannel(l2.info['id'], FUNDAMOUNT, feerate="250perkb")
    assert funding['txid'] in l1.bitcoin.rpc.getrawmempool()
    funding_txid = funding['txid']
    funding_vout = funding['outnum']

    # Get funding transaction details to identify change output
    tx_details = l1.bitcoin.rpc.getrawtransaction(funding_txid, True)
    change_vout = None
    for vout in range(len(tx_details['vout'])):
        if vout != funding_vout:  # Change is the non-funding vout
            change_vout = vout
            break
    assert change_vout is not None, f"No change vout found in funding tx {funding_txid}"
    change_utxo = {
        'txid': funding_txid,
        'vout': change_vout,
        'amount_msat': int(float(tx_details['vout'][change_vout]['value']) * 1e8) * 1000
    }

    funds = l1.rpc.listfunds()
    total_balance = sum(output["amount_msat"] / 1000 for output in funds["outputs"])
    print(f"Total balance: {total_balance} sats")
    assert total_balance >= EMERGENCY_RESERVE, f"Wallet balance {total_balance} below {EMERGENCY_RESERVE}"

    # Verify change UTXO in listfunds
    available_utxos = [utxo for utxo in funds["outputs"] if not utxo.get("reserved", False) and utxo["txid"] == change_utxo["txid"] and utxo["output"] == change_utxo["vout"]]
    assert len(available_utxos) == 1, f"Expected 1 change UTXO, got {len(available_utxos)}"
    current_unreserved = sum(utxo["amount_msat"] / 1000 for utxo in available_utxos)
    print(f"Change UTXO balance (txid={change_utxo['txid']}, vout={change_utxo['vout']}): {current_unreserved} sats")

    # Test: Bump using change UTXO with fixed fee to leave exactly 24,999 sats
    utxo = available_utxos[0]
    fixed_fee = int(current_unreserved - 24999)  # Fee to leave exactly 24,999 sats
    print(f"Paying CPFP with: txid={utxo['txid']}, vout={utxo['output']}, amount={utxo['amount_msat']/1000} sats, fixed fee={fixed_fee} sats")
    result = l1.rpc.bumpchannelopen(txid=utxo["txid"], vout=utxo["output"], amount=f"{fixed_fee}sats")

    # Sanity check to make sure we are not spending our emergency reserve
    leftover_emergencyreserve = change_utxo['amount_msat'] // 1000 - int(result['child_fee'])
    assert leftover_emergencyreserve == 24999, f"Expected 24,999 sats left, got {leftover_emergencyreserve}"
    assert "code" in result and result["code"] == -32600, f"Expected reserve error, got {result}"
    assert "reserve" in result["message"].lower(), f"Expected reserve message, got {result['message']}"
    print(f"Success: Reserve protected with fixed fee: {result['message']}")

    # Clean up: Unreserve inputs if reserved
    if "unreserve_inputs_command" in result:
        l1.rpc.unreserveinputs(result["unreserve_inputs_command"].split()[-1])
        print("Unreserved inputs after test")
        