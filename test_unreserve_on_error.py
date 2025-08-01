import os
from pyln.client import Plugin, RpcError
from pyln.testing.fixtures import *  # noqa: F403
from pyln.testing.utils import sync_blockheight, BITCOIND_CONFIG, FUNDAMOUNT
from bitcointx.core.psbt import PartiallySignedTransaction

pluginopt = {'plugin': os.path.join(os.path.dirname(__file__), "bumpit.py")}
FUNDAMOUNT = 1000000


plugin = Plugin()


def connect_bitcoincli(rpc_user="__cookie__", rpc_password=None, host="127.0.0.1", port=18443):
    """
    Connects to a Bitcoin Core RPC server.

    Args:
        rpc_user (str): The RPC username, default is '__cookie__' for cookie authentication.
        rpc_password (str): The RPC password or cookie value (default: None).
        host (str): The RPC host, default is '127.0.0.1'.
        port (int): The RPC port, default is 18443.

    Returns:
        AuthServiceProxy: The RPC connection object.
    """
    if rpc_password is None:
        # Attempt to retrieve the cookie value from the regtest .cookie file
        try:
            cookie_path = os.path.expanduser("~/.bitcoin/regtest/.cookie")
            with open(cookie_path, "r") as cookie_file:
                rpc_user, rpc_password = cookie_file.read().strip().split(":")
        except FileNotFoundError:
            raise FileNotFoundError("Could not find the .cookie file. Ensure Bitcoin Core is running with cookie-based auth enabled.")


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

    # # Connect nodes and fund
    l1.rpc.connect(l2.info['id'], 'localhost', l2.port)
    bitcoind = l1.bitcoin
    addr = l1.rpc.newaddr()['bech32']
    bitcoind.rpc.sendtoaddress(addr, 2)  # 2 btc for enough funds
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


    # Plugin configuration options
    plugin.add_option('bump_brpc_user', None, 'bitcoin rpc user')
    plugin.add_option('bump_brpc_pass', None, 'bitcoin rpc password')
    plugin.add_option('bump_brpc_port', 18443, 'bitcoin rpc port')
    plugin.add_option(
        "yolo",
        None,
        "Set to 'yolo' to broadcast transaction automatically after finalizing the psbt"
    )

    rpc_connection = connect_bitcoincli(
        rpc_user=plugin.get_option('bump_brpc_user'),
        rpc_password=plugin.get_option('bump_brpc_pass'),
        port=plugin.get_option('bump_brpc_port')
    )   

    addr2 = l1.rpc.newaddr()['bech32']

    mock_psbt = rpc_connection.createpsbt([{"txid":funding_txid, "vout":change_output}], [{addr2:0.000000300}])
    reserved_input = plugin.rpc.reserveinputs(mock_psbt)


    # Call bumpchannelopen, should break when creating the real psbt since the input should already be reserved
    try:
        result = l1.rpc.bumpchannelopen(
            txid=funding_txid,
            vout=change_output['output'],
            amount="1000sats"
        )
        print(f"Unexpected success: {result}")
        assert False, "Expected an error but bumpchannelopen succeeded"
    except RpcError as e:
        print(f"Expected error response: {str(e)}")
        assert "Failed to reserve or sign PSBT" in str(e) or "Could not extract hex from finalized PSBT" in str(e), f"Unexpected error: {e}"


    