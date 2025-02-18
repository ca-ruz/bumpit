#!/usr/bin/env python3
from pyln.client import Plugin, RpcError
import json
from bitcointx.core.psbt import PartiallySignedTransaction
from bitcoinrpc.authproxy import AuthServiceProxy, JSONRPCException
import os

plugin = Plugin()

class CPFPError(Exception):
    """Custom exception for CPFP-related errors"""
    pass

# Plugin configuration options
plugin.add_option('bump_brpc_user', None, 'bitcoin rpc user')
plugin.add_option('bump_brpc_pass', None, 'bitcoin rpc password')
plugin.add_option('bump_brpc_port', 18443, 'bitcoin rpc port')

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
    
    rpc_url = f"http://{rpc_user}:{rpc_password}@{host}:{port}"
    plugin.log(f"[ALPHA] Connecting to Bitcoin Core at: {rpc_url}")
    
    try:
        return AuthServiceProxy(rpc_url)
    except Exception as e:
        raise ConnectionError(f"Error connecting to Bitcoin Core: {e}")

def calculate_confirmed_unreserved_amount(funds_data):
    """
    Calculates total amount in satoshis from confirmed and unreserved outputs.
    """
    total_sats = 0
    if "outputs" in funds_data:
        for output in funds_data["outputs"]:
            if output.get("status") == "confirmed" and not output.get("reserved", False):
                total_sats += output.get("amount_msat", 0) // 1000
    return total_sats

def calculate_child_fee(parent_fee, parent_vsize, child_vsize, desired_total_feerate):
    """
    Calculates the required child transaction fee to achieve the desired total feerate.

    :param parent_fee: Fee paid by the parent transaction (in satoshis).
    :param parent_vsize: Size of the parent transaction (in vbytes).
    :param child_vsize: Size of the child transaction (in vbytes).
    :param desired_total_feerate: Desired total feerate (in sat/vB).
    :return: The required child transaction fee (in satoshis).
    """
    # Calculate the total required fee for both transactions combined
    total_vsize = parent_vsize + child_vsize
    required_total_fee = desired_total_feerate * total_vsize
    
    # Calculate how much the child needs to pay to achieve the desired total feerate
    child_fee = required_total_fee - parent_fee
    
    # Ensure the child fee is at least enough to meet minimum relay fee
    return max(child_fee, 0)


@plugin.method("bumpchannelopen")
def bumpchannelopen(plugin, txid, vout, fee_rate, address, **kwargs):
    """
    Creates a CPFP transaction for a specific parent output.
    
    Args:
        txid: Parent transaction ID
        vout: Output index
        fee_rate: Desired fee rate in sat/vB
        address: Destination address for change
    """
    # Input validation
    if not txid or vout is None:
        raise CPFPError("Both txid and vout are required.")

    plugin.log(f"[BRAVO] Input Parameters - txid: {txid}, vout: {vout}, fee_rate: {fee_rate}, address: {address}")

    # Step 1: Fetch the network information from the Lightning node
    info = plugin.rpc.getinfo()
    network = info.get('network')
    plugin.log(f"[CHARLIE] Network detected: {network}")

    if not network:
        raise CPFPError("Network information is missing.")
    plugin.log(f"[DELTA] Network detected: {network}")

    # Step 2: Get list of available UTXOs from the Lightning node
    funds = plugin.rpc.listfunds()
    utxos = funds.get("outputs", [])
    if not utxos:
        raise CPFPError("No unspent transaction outputs found.")

    plugin.log("[ECHO] Available UTXOs:")
    for idx, utxo in enumerate(utxos):
        plugin.log(f"[FOXTROT] {idx}: txid={utxo['txid']} vout={utxo['output']} amount={utxo['amount_msat']} msat")


    # Step 3: Calculate the total amount of confirmed and unreserved outputs
    total_sats = calculate_confirmed_unreserved_amount(funds)
    plugin.log(f"[GOLF] Total amount in confirmed and unreserved outputs: {total_sats} sats")

    # Step 4: Try to select an available, unreserved UTXO
    selected_utxo = None
    for utxo in utxos:
        if utxo["txid"] == txid and utxo["output"] == vout:
            if utxo.get("reserved", False):
                raise CPFPError(f"Selected utxo {txid}:{vout} is reserved.")
            else:
                selected_utxo = utxo
                break

    if not selected_utxo:
        raise CPFPError(f"UTXO {txid}:{vout} not found.")
    plugin.log(f"[HOTEL] Selected UTXO: txid={selected_utxo['txid']}, vout={selected_utxo['output']}")
    plugin.log(f"[INDIA] Contents of selected_utxo: {selected_utxo}")

    # Step 5: Fetch UTXO details and convert amount
    amount_msat = selected_utxo["amount_msat"]
    if not amount_msat:
        raise CPFPError(f"UTXO {txid}:{vout} not found or already spent.")
    plugin.log(f"[JULIET] amount_msat type: {type(amount_msat)}, value: {amount_msat}")

    plugin.log(f"[KILO] txid variable contains this txid: {txid}")

    amount = amount_msat // 1000  # Convert msat to satoshis
    plugin.log(f"[LIMA] Fetched UTXO: txid={selected_utxo['txid']}, vout={selected_utxo['output']}, amount={amount} sats")

    # Step 6: Use `txprepare` to create and broadcast the transaction
    utxo_selector = [f"{selected_utxo['txid']}:{selected_utxo['output']}"]
    plugin.log(f"[MIKE] Bumping selected output using UTXO {utxo_selector}")



    try:
        # First time we call txprepare with 0 receiving amount
        rpc_result = plugin.rpc.txprepare(
            outputs=[{address: 0}],
            utxos=utxo_selector,
            feerate=fee_rate
        )

        plugin.log(f"[NOVEMBER] rpc_result: {rpc_result}")
        plugin.log(f"[OSCAR] feerate: {fee_rate}")

        v0_psbt = plugin.rpc.setpsbtversion(
            psbt=rpc_result.get("psbt"),
            version=0
        )
        plugin.log(f"[PAPA] v0_psbt: {v0_psbt}")

        new_psbt= PartiallySignedTransaction.from_base64(v0_psbt.get("psbt"))

        fee = new_psbt.get_fee()
        plugin.log(f"[QUEBEC] psbt first_child fee: {fee}")

        plugin.rpc.unreserveinputs(
            psbt=rpc_result.get("psbt"),
        )

    except CPFPError as e:
        plugin.log(f"[ROMEO] CPFPError occurred: {str(e)}")
        raise CPFPError("Error creating CPFP transaction.")
    except RpcError as e:
        plugin.log(f"[SIERRA] RPC Error during withdrawal: {str(e)}")
        raise CPFPError(f"RPC Error while withdrawing funds: {str(e)}")
    except Exception as e:
        plugin.log(f"[TANGO] General error occurred while withdrawing: {str(e)}")
        raise CPFPError(f"Error while withdrawing funds: {str(e)}")



    # Emergency channel amount in sats, cln will create an output of this amount
    # as long as we subtract it from the recipient amount
    emergency_refill_amount = 0
    if total_sats < 25000:
        emergency_refill_amount = 25000 - total_sats

    if amount <= emergency_refill_amount:
        raise CPFPError("Not enough funds for fees and emergency reserve.")

    recipient_amount = amount - emergency_refill_amount - fee # Subtract emergency channel
    plugin.log(f"[UNIFORM] Reserve amount: {emergency_refill_amount} sats, Recipient amount: {recipient_amount} sats")
    plugin.log(f"[VICTOR] fee: {fee}")
        # First attempt using the bitcoin rpc_connection function:

    rpc_connection = connect_bitcoincli(
        rpc_user=plugin.get_option('bump_brpc_user'),
        rpc_password=plugin.get_option('bump_brpc_pass'),
        port=plugin.get_option('bump_brpc_port')
    )

    # Hardcoded values, user should pass in their host, port, rpcuser and rpcpassword
    # rpc_connection = AuthServiceProxy("http://%s:%s@127.0.0.1:18443"%("__cookie__", "12bacf16e6963c18ddfe8fe18ac275300d1ea40ed4738216d89bcf3a1b707ed3"))
    tx = rpc_connection.getrawtransaction(txid, True)
    # Calculate total inputs
    total_inputs = 0
    for vin in tx["vin"]:
        input_tx = rpc_connection.getrawtransaction(vin["txid"], True)
        total_inputs += input_tx["vout"][vin["vout"]]["value"]
    # Calculate total outputs
    total_outputs = sum(vout["value"] for vout in tx["vout"])
    # Calculate the fee
    parent_fee = total_inputs - total_outputs
    parent_fee = parent_fee * 10**8
    
    # Get parent transaction size
    parent_tx_hex = rpc_connection.getrawtransaction(txid)
    parent_tx_dict = rpc_connection.decoderawtransaction(parent_tx_hex)
    parent_vsize = parent_tx_dict.get("vsize")
    plugin.log(f"[WHISKEY] Contents of parent_vsize: {parent_vsize}")
    parent_fee_rate = parent_fee / parent_vsize  # sat/vB
    plugin.log(f"[XRAY] Contents of parent_fee_rate: {parent_fee_rate}")
    plugin.log(f"[YANKEE] Contents of parent_fee: {parent_fee}")


    # Second time we call txprepare
    try:
        second_rpc_result = plugin.rpc.txprepare(
            outputs=[{address: recipient_amount}],
            utxos=utxo_selector,
            feerate=fee_rate
        )

        plugin.log(f"[ZULU] second_rpc_result: {second_rpc_result}")
        plugin.log(f"[ALPHA-ALPHA] second_feerate: {fee_rate}")

        second_v0_psbt = plugin.rpc.setpsbtversion(
            psbt=second_rpc_result.get("psbt"),
            version=0
        )
        plugin.log(f"[ALPHA-BRAVO] second_v0_psbt: {second_v0_psbt}")

        second_new_psbt= PartiallySignedTransaction.from_base64(second_v0_psbt.get("psbt"))

        second_fee = second_new_psbt.get_fee()
        plugin.log(f"[ALPHA-CHARLIE] psbt second_fee: {second_fee}")

        # TODO Uncommented for testing, maybe comment back till the next TODO

        # plugin.rpc.unreserveinputs(
        #     psbt=second_rpc_result.get("psbt"),
        # )

        # TODO

        # plugin.rpc.unreserveinputs(
        #     psbt=second_rpc_result.get("psbt"),
        # )

    #     # second_rpc_result = plugin.rpc.withdraw(
    #     #     destination=address,
    #     #     satoshi=recipient_amount,
    #     #     feerate=fee_rate,
    #     #     utxos=utxo_selector
    #     # )

        # plugin.log(f"second_rpc_result: {json.dumps(second_rpc_result, indent=4)}")  # Log the full result

    except CPFPError as e:
        plugin.log(f"[ALPHA-JULIET] CPFPError occurred: {str(e)}")
        raise CPFPError("Error creating CPFP transaction.")
    except RpcError as e:
        plugin.log(f"[ALPHA-KILO] RPC Error during withdrawal: {str(e)}")
        raise CPFPError(f"RPC Error while withdrawing funds: {str(e)}")
    except Exception as e:
        plugin.log(f"[ALPHA-LIMA] General error occurred while withdrawing: {str(e)}")
        raise CPFPError(f"Error while withdrawing funds: {str(e)}")

    # Step 9: Log and return the transaction details
    first_child = rpc_result.get("txid")
    first_psbt = rpc_result.get("psbt")
    first_signed_psbt = ""

    # txid contains a new txid
    plugin.log(f"[ALPHA-HOTEL] txid variable contains this txid: {txid}")
    plugin.log(f"[ALPHA-INDIA] first_child variable contains this txid: {first_child}")

    # plugin.log(f"Broadcasted CPFP transaction with txid: {txid}")

    try:
            first_signed_v2_psbt = plugin.rpc.signpsbt(
                psbt=first_psbt
            )

            first_signed_v0_psbt = plugin.rpc.setpsbtversion(
                psbt=first_signed_v2_psbt.get("signed_psbt"),
                version=0
            )
            first_child_v0_psbt = first_signed_v0_psbt.get("psbt")
            first_psbt_v0 = "'" + first_child_v0_psbt + "'"
            first_psbt_v2 = first_signed_v2_psbt.get("signed_psbt")
            plugin.log(f"[ALPHA-WHISKEY] Contents of rpc_connection: {rpc_connection}")
            first_child_analyzed = rpc_connection.analyzepsbt(first_child_v0_psbt)
            first_child_fee = first_child_analyzed["fee"]
            first_child_vsize = first_child_analyzed["estimated_vsize"]
            first_child_feerate = first_child_analyzed["estimated_feerate"]
            plugin.log(f"[ALPHA-ECHO] Contents of first_child_fee: {first_child_fee}")
            plugin.log(f"[ALPHA-FOXTROT] Contents of first_child_vsize: {first_child_vsize}")
            plugin.log(f"[ALPHA-GOLF] Contents of first_child_feerate: {first_child_feerate}")

            # first_total_vsizes = parent_vsize + child_vsize
            # plugin.log(f"Contents of total_vsizes: {total_vsizes}")
            # first_total_fees = (parent_fee + child_fee) * 10**8  # Convert fees to satoshis if in BTC
            # plugin.log(f"Contents of total_fees: {total_fees}")
            # first_total_feerate = total_fees / total_vsizes
            # plugin.log(f"Contents of total_feerate: {total_feerate}")

            # plugin.log(f"Signed PSBT (v2): {signed_v2_psbt}")
            # plugin.log(f"Signed PSBT (v0): {signed_v0_psbt}")


            # TODO maybe uncomment this again? just did it because it seems to be breaking the plugin since the next time we try
            # get an error message saying that the UTXO is not reserved, til the next TODO

            # plugin.rpc.unreserveinputs(
            #     psbt=rpc_result.get("psbt"),
            # )

            # TODO

        #     # second_rpc_result = plugin.rpc.withdraw(
        #     #     destination=address,
        #     #     satoshi=recipient_amount,
        #     #     feerate=fee_rate,
        #     #     utxos=utxo_selector
        #     # )

            # plugin.log(f"second_rpc_result: {json.dumps(second_rpc_result, indent=4)}")  # Log the full result

    except CPFPError as e:
        plugin.log(f"[ALPHA-JULIET] CPFPError occurred: {str(e)}")
        raise CPFPError("Error creating CPFP transaction.")
    except RpcError as e:
        plugin.log(f"[ALPHA-KILO] RPC Error during withdrawal: {str(e)}")
        raise CPFPError(f"RPC Error while withdrawing funds: {str(e)}")
    except Exception as e:
        plugin.log(f"[ALPHA-LIMA] General error occurred while withdrawing: {str(e)}")
        raise CPFPError(f"Error while withdrawing funds: {str(e)}")


    second_child_txid = second_rpc_result.get("txid")
    second_psbt = second_rpc_result.get("psbt")
    second_signed_psbt = ""
    
    # txid contains a new txid    
    plugin.log(f"[ALPHA-HOTEL] txid variable contains this txid: {txid}")
    plugin.log(f"[ALPHA-INDIA] first_child variable contains this txid: {first_child}")

    # plugin.log(f"Broadcasted CPFP transaction with txid: {txid}")

    # TODO Uncomment this part of the code til the next TODO

    try:
        second_signed_v2_psbt = plugin.rpc.signpsbt(
            psbt=second_psbt
        )

        second_signed_v0_psbt = plugin.rpc.setpsbtversion(
            psbt=second_signed_v2_psbt.get("signed_psbt"),
            version=0
        )
        second_child_v0_psbt = second_signed_v0_psbt.get("psbt")
        second_psbt_v0 = "'" + second_child_v0_psbt + "'"
        second_psbt_v2 = second_signed_v2_psbt.get("signed_psbt")
        plugin.log(f"[ALPHA-WHISKEY] Contents of rpc_connection: {rpc_connection}")
        second_child_analyzed = rpc_connection.analyzepsbt(second_child_v0_psbt)
        second_child_fee = second_child_analyzed["fee"]
        second_child_vsize = second_child_analyzed["estimated_vsize"]
        second_child_feerate = second_child_analyzed["estimated_feerate"]
        plugin.log(f"[ALPHA-MIKE] Contents of second_child_fee: {second_child_fee}")
        plugin.log(f"[ALPHA-NOVEMBER] Contents of second_child_vsize: {second_child_vsize}")
        plugin.log(f"[ALPHA-OSCAR] Contents of second_child_feerate: {second_child_feerate}")

        # TODO Maybe uncomment this later, till the next TODO


        # TODO

        plugin.rpc.unreserveinputs(
            psbt=second_rpc_result.get("psbt"),
        )

        child_fee = calculate_child_fee(parent_fee, parent_vsize, second_child_vsize, fee_rate)
        print(f"Child transaction fee: {child_fee} satoshis")
        plugin.log(f"[ALPHA-PAPA] line 547: child_fee variable contains: {child_fee}")

        child_fee_rate = child_fee / second_child_vsize  # sat/vB
        plugin.log(f"[ALPHA-QUEBEC] line 438: child_fee_rate variable contains: {child_fee_rate}")

        plugin.log(f"[ALPHA-ROMEO] second_rpc_result: {json.dumps(second_rpc_result, indent=4)}")  # Log the full result

        total_vsizes = parent_vsize + second_child_vsize
        plugin.log(f"[ALPHA-SIERRA] Contents of total_vsizes: {total_vsizes}")
        plugin.log(f"[ALPHA-SIERRA-B] Contents of parent_fee: {parent_fee}")
        plugin.log(f"[ALPHA-SIERRA-C] Contents of child_fee: {child_fee}")
        total_fees = (parent_fee + child_fee)  # Convert fees to satoshis if in BTC
        plugin.log(f"[ALPHA-TANGO] Contents of total_fees: {total_fees}")
        total_feerate = total_fees / total_vsizes
        plugin.log(f"[ALPHA-UNIFORM] Contents of total_feerate: {total_feerate}")

        plugin.log(f"[ALPHA-VICTOR] Signed PSBT (v2): {second_signed_v2_psbt}")
        plugin.log(f"[ALPHA-WHISKEY] Signed PSBT (v0): {second_signed_v0_psbt}")
    except CPFPError as e:
        plugin.log(f"[ALPHA-JULIET] CPFPError occurred: {str(e)}")
        raise CPFPError("Error creating CPFP transaction.")
    except RpcError as e:
        plugin.log(f"[ALPHA-KILO] RPC Error during withdrawal: {str(e)}")
        raise CPFPError(f"RPC Error while withdrawing funds: {str(e)}")
    except Exception as e:
        plugin.log(f"[ALPHA-LIMA] General error occurred while withdrawing: {str(e)}")
        raise CPFPError(f"Error while withdrawing funds: {str(e)}")




    # total_fee_rate = parent_fee_rate + child_fee_rate
    # plugin.log(f"line 553: total_fee_rate variable contains: {total_fee_rate}")

    # TODO

    # child_v0_psbt = signed_v0_psbt.get("psbt")
    # psbt_v0 = "'" + child_v0_psbt + "'"
    # psbt_v2 = signed_v2_psbt.get("signed_psbt")

    # TODO Uncomment this next bit till the next TODO

    # Prepare the final response
    response = {
        "message": "Please make sure to run bitcoin-cli finalizepsbt and analyzepsbt to verify "
        "the details before broadcasting the transaction",
        "finalize command": f'copy/paste this: bitcoin-cli finalizepsbt {second_psbt_v0} ',
        "analyze command": f'copy/paste this: bitcoin-cli analyzepsbt {second_psbt_v0} ',
        "signed_v2_psbt": second_psbt_v2,
        # "total_vsizes": total_vsizes,
        # "total_fees": total_fees,
        # "total_feerate": total_feerate
    }

    # TODO

    plugin.log(f"[BRAVO-ALPHA] line 556: txid variable contains this txid: {txid}")
    plugin.log(f"[BRAVO-BRAVO] line 557: second_child_txid variable contains this txid: {second_child_txid}")

 





    # Update the dictionary with new key-value pairs & Convert non-serializable objects to serializable formats
    response.update({
        "total_vsizes": int(total_vsizes) if total_vsizes is not None else 0,
        "total_fees": int(total_fees) if total_fees is not None else 0,
        "total_feerate": float(total_feerate) if total_feerate is not None else 0.0
    })



    # # Prepare the final response
    # response = {
    #     "message": "Please make sure to run bitcoin-cli finalizepsbt and analyzepsbt to verify "
    #     "the details before broadcasting the transaction",
    #     "finalize command": f'copy/paste this: bitcoin-cli finalizepsbt {psbt_v0} ',
    #     "analyze command": f'copy/paste this: bitcoin-cli analyzepsbt {psbt_v0} ',
    #     "signed_v2_psbt": psbt_v2,
    #     "total_vsizes": total_vsizes,
    #     "total_fees": total_fees,
    #     "total_feerate": total_feerate
    # }

    # TODO Uncomment this next bit out till the next TODO to replace the return

    return response

plugin.run()
