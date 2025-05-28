import os
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, BITCOIND_CONFIG

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}
FUNDAMOUNT = 74000  # Channel funding amount in satoshis
INITIAL_FUNDING = 100000  # Initial wallet funding in satoshis
EMERGENCY_RESERVE = 25000  # Minimum wallet balance in satoshis

def test_emergency_reserve(node_factory):
    opts = {
        'bump_brpc_user': BITCOIND_CONFIG["rpcuser"],
        'bump_brpc_pass': BITCOIND_CONFIG["rpcpassword"],
        'bump_brpc_port': BITCOIND_CONFIG["rpcport"]
    }
    opts.update(pluginopt)
    l1, l2 = node_factory.get_nodes(2, opts=opts)

    # Fund l1's wallet with 100,000 sats
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    addr = l1.rpc.newaddr()['bech32']
    l1.bitcoin.rpc.sendtoaddress(addr, INITIAL_FUNDING / 1e8)
    l1.bitcoin.generate_block(1)
    sync_blockheight(l1.bitcoin, [l1, l2])

    # Open a channel with 74,000 sats (unconfirmed)
    funding = l1.rpc.fundchannel(l2.info['id'], FUNDAMOUNT, feerate="250perkb")
    assert funding['txid'] in l1.bitcoin.rpc.getrawmempool()

    # Verify wallet balance is at least 25,000 sats
    funds = l1.rpc.listfunds()
    total_balance = sum(output["amount_msat"] / 1000 for output in funds["outputs"])
    assert total_balance >= EMERGENCY_RESERVE, f"Wallet balance {total_balance} sats below reserve {EMERGENCY_RESERVE} sats"

    # Select a non-reserved UTXO
    available_utxos = [utxo for utxo in funds["outputs"] if not utxo.get("reserved", False)]
    if not available_utxos:
        print(f"Success: No non-reserved UTXOs available, reserve protected by CLN (balance: {total_balance} sats)")
        return

    utxo = available_utxos[0]
    result = l1.rpc.bumpchannelopen(txid=utxo["txid"], vout=utxo["output"], fee_rate=3)

    # Assert bump fails to protect reserve
    assert "code" in result and result["code"] == -32600, f"Expected reserve error, got {result}"
    assert "reserve" in result["message"].lower(), f"Expected reserve violation message, got {result['message']}"
    print(f"Success: Emergency reserve protected: {result['message']}")
    