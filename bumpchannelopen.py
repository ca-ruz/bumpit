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
    #plugin.log(f"[ALPHA] Connecting to Bitcoin Core at: {rpc_url}")
    
    try:
        return AuthServiceProxy(rpc_url)
    except Exception as e:
        raise ConnectionError(f"Error connecting to Bitcoin Core: {e}")

def calculate_confirmed_unreserved_amount(funds_data, txid, vout):
    """
    Calculates total amount in satoshis from confirmed and unreserved outputs except the output being bumped.
    """
    total_sats = 0
    if "outputs" in funds_data:
        for output in funds_data["outputs"]:
            if output.get("txid") == txid and output.get("output") == vout:
                continue
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
    return child_fee


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
    plugin.log(f"[INFO] Funds retrieved: {funds}")  # Log the entire funds response at INFO level

    # Check if 'outputs' key exists and log its contents
    utxos = funds.get("outputs", [])
    if not utxos:
        raise CPFPError("No unspent transaction outputs found.")

    # Log all UTXOs before filtering (optional, can be removed for cleaner logs)
    # plugin.log("[DEBUG] All UTXOs before filtering:")
    # for idx, utxo in enumerate(utxos):
    #     reserved_status = utxo.get("reserved", False)
    #     plugin.log(f"[DEBUG] UTXO {idx}: txid={utxo['txid']} vout={utxo['output']} amount={utxo['amount_msat']} msat, reserved={reserved_status}")

    # Filter out reserved UTXOs
    available_utxos = [utxo for utxo in utxos if not utxo.get("reserved", False)]

    # Log available UTXOs after filtering
    plugin.log("[INFO] Available UTXOs after filtering:")
    
    # Add the specific log message you requested
    plugin.log("[ECHO] Available UTXOs after filtering:")
    if not available_utxos:
        plugin.log("[ECHO] No unreserved UTXOs available.")
    else:
        for idx, utxo in enumerate(available_utxos):
            plugin.log(f"[FOXTROT] {idx}: txid={utxo['txid']} vout={utxo['output']} amount={utxo['amount_msat']} msat")

    # Log the count of available UTXOs
    plugin.log(f"[DEBUG] Count of available UTXOs: {len(available_utxos)}")

    # Check if available UTXOs are being logged correctly
    if available_utxos:
        plugin.log(f"[DEBUG] Available UTXOs contents: {available_utxos}")

    if not available_utxos:
        raise CPFPError("No unreserved unspent transaction outputs found.")

    # Proceed with selecting a UTXO
    selected_utxo = None
    for utxo in available_utxos:
        if utxo["txid"] == txid and utxo["output"] == vout:
            selected_utxo = utxo
            break

    if not selected_utxo:
        raise CPFPError(f"UTXO {txid}:{vout} not found in available UTXOs.")

    # Log the selected UTXO
    plugin.log(f"[DEBUG] Selected UTXO: txid={selected_utxo['txid']}, vout={selected_utxo['output']}, amount={selected_utxo['amount_msat']} msat")

    # Step 3: Calculate the total amount of confirmed and unreserved outputs
    total_sats = calculate_confirmed_unreserved_amount(funds, txid, vout)
    emergency_refill_amount = max(25000 - total_sats, 0)  # Ensures non-negative value
    plugin.log(f"[GOLF] Total amount in confirmed and unreserved outputs: {total_sats} sats")
    plugin.log(f"[GOLF 1.5] Total amount in emergency_refill_amount: {emergency_refill_amount} sats")

    # Step 4: Fetch UTXO details and convert amount
    amount_msat = selected_utxo["amount_msat"]
    if not amount_msat:
        raise CPFPError(f"UTXO {txid}:{vout} not found or already spent.")

    # Log the amount in msat and convert to sats
    amount = amount_msat // 1000  # Convert msat to satoshis
    plugin.log(f"[DEBUG] Amount in sats: {amount} sats")


    # Step 6: Use `txprepare` to create and broadcast the transaction
    utxo_selector = [f"{selected_utxo['txid']}:{selected_utxo['output']}"]
    plugin.log(f"[MIKE] Bumping selected output using UTXO {utxo_selector}")

    try:
        # First time we call txprepare with 0 receiving amount
        rpc_result = plugin.rpc.txprepare(
            outputs=[{address: 0}],
            utxos=utxo_selector,
            #feerate=10000
        )



        plugin.log(f"[NOVEMBER] rpc_result: {rpc_result}")

        v0_psbt = plugin.rpc.setpsbtversion(
            psbt=rpc_result.get("psbt"),
            version=0
        )
        plugin.log(f"[PAPA] v0_psbt: {v0_psbt}")

        new_psbt= PartiallySignedTransaction.from_base64(v0_psbt.get("psbt"))

        # First child transation fee
        fee = new_psbt.get_fee()
        plugin.log(f"[QUEBEC] psbt first_child fee: {fee}")



        rpc_connection = connect_bitcoincli(
            rpc_user=plugin.get_option('bump_brpc_user'),
            rpc_password=plugin.get_option('bump_brpc_pass'),
            port=plugin.get_option('bump_brpc_port')
        )

        # Step 9: Log and return the transaction details
        first_child = rpc_result.get("txid")
        first_psbt = rpc_result.get("psbt")
        first_signed_v2_psbt = ""

        # txid contains a txid passed in by the user as an argument when calling the method
        plugin.log(f"[ALPHA-HOTEL] txid variable contains this txid: {txid}")
        plugin.log(f"[ALPHA-INDIA] first_child variable contains this txid: {first_child}")

        try:
            first_signed_v2_psbt = plugin.rpc.signpsbt(
                psbt=first_psbt
            )

            first_signed_v0_psbt = plugin.rpc.setpsbtversion(
                psbt=first_signed_v2_psbt.get("signed_psbt"),
                version=0
            )

            first_child_v0_psbt = first_signed_v0_psbt.get("psbt")

            # Variable not in use first_child_v2_psbt
            first_child_v2_psbt = first_signed_v2_psbt.get("signed_psbt")

            first_child_analyzed = rpc_connection.analyzepsbt(first_child_v0_psbt)
            first_child_fee = first_child_analyzed["fee"]
            first_child_vsize = first_child_analyzed["estimated_vsize"]
            first_child_feerate = first_child_analyzed["estimated_feerate"]
            plugin.log(f"[ALPHA-ECHO] Contents of first_child_fee: {first_child_fee}")
            plugin.log(f"[ALPHA-FOXTROT] Contents of first_child_vsize: {first_child_vsize}")
            plugin.log(f"[ALPHA-GOLF] Contents of first_child_feerate: {first_child_feerate}")

        except CPFPError as e:
            plugin.log(f"[ALPHA-JULIET] CPFPError occurred: {str(e)}")
            raise CPFPError("Error creating CPFP transaction.")
        except RpcError as e:
            plugin.log(f"[ALPHA-KILO] RPC Error during withdrawal: {str(e)}")
            raise CPFPError(f"RPC Error while withdrawing funds: {str(e)}")
        except Exception as e:
            plugin.log(f"[ALPHA-LIMA] General error occurred while withdrawing: {str(e)}")
            raise CPFPError(f"Error while withdrawing funds: {str(e)}")

        

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


    rpc_connection = connect_bitcoincli(
        rpc_user=plugin.get_option('bump_brpc_user'),
        rpc_password=plugin.get_option('bump_brpc_pass'),
        port=plugin.get_option('bump_brpc_port')
    )




    # Get parent's tx info

    # Hardcoded values, user should pass in their host, port, rpcuser and rpcpassword
    # rpc_connection = AuthServiceProxy("http://%s:%s@127.0.0.1:18443"%("__cookie__", "12bacf16e6963c18ddfe8fe18ac275300d1ea40ed4738216d89bcf3a1b707ed3"))
    tx = rpc_connection.getrawtransaction(txid, True)
    plugin.log(f"[TANGO - WHISKEY] Contents tx: {tx}")



    # Calculate total inputs
    total_inputs = 0
    for vin in tx["vin"]:
        input_tx = rpc_connection.getrawtransaction(vin["txid"], True)
        total_inputs += input_tx["vout"][vin["vout"]]["value"]

    plugin.log(f"[TANGO - WHISKEY 2] Contents of total_inputs: {total_inputs}")

    # Calculate total outputs
    total_outputs = sum(vout["value"] for vout in tx["vout"])

    plugin.log(f"[TANGO - WHISKEY 3] Contents of total_outputs: {total_outputs}")

    # Calculate the fee
    parent_fee = total_inputs - total_outputs
    parent_fee = parent_fee * 10**8
    plugin.log(f"[TANGO - WHISKEY 4] Contents of parent_fee: {parent_fee}")

    # Get parent transaction size
    parent_tx_hex = rpc_connection.getrawtransaction(txid)
    parent_tx_dict = rpc_connection.decoderawtransaction(parent_tx_hex)
    parent_vsize = parent_tx_dict.get("vsize")
    plugin.log(f"[WHISKEY] Contents of parent_vsize: {parent_vsize}")

    parent_fee_rate = parent_fee / parent_vsize  # sat/vB
    plugin.log(f"[YANKEE] Contents of parent_fee_rate: {parent_fee_rate}")


    # desired_child_fee = calculate_child_fee(
    #     parent_fee,
    #     parent_vsize,
    #     first_child_vsize, 
    #     fee_rate
    # )

    # # Emergency channel amount in sats, cln will create an output of this amount
    # # as long as we subtract it from the recipient amount
    # emergency_refill_amount = max(25000 - total_sats, 0)  # Ensures non-negative value

    # if (emergency_refill_amount + desired_child_fee) >= amount:
    #     raise CPFPError(f"Not enough funds: UTXO has {amount} sats, needs {emergency_refill_amount + desired_child_fee} sats (fee + reserve)")



    # rpc_connection = connect_bitcoincli(
    #     rpc_user=plugin.get_option('bump_brpc_user'),
    #     rpc_password=plugin.get_option('bump_brpc_pass'),
    #     port=plugin.get_option('bump_brpc_port')
    # )

    # # Hardcoded values, user should pass in their host, port, rpcuser and rpcpassword
    # # rpc_connection = AuthServiceProxy("http://%s:%s@127.0.0.1:18443"%("__cookie__", "12bacf16e6963c18ddfe8fe18ac275300d1ea40ed4738216d89bcf3a1b707ed3"))
    # tx = rpc_connection.getrawtransaction(txid, True)
    # # Calculate total inputs
    # total_inputs = 0
    # for vin in tx["vin"]:
    #     input_tx = rpc_connection.getrawtransaction(vin["txid"], True)
    #     total_inputs += input_tx["vout"][vin["vout"]]["value"]
    # # Calculate total outputs
    # total_outputs = sum(vout["value"] for vout in tx["vout"])
    # # Calculate the fee
    # parent_fee = total_inputs - total_outputs
    # parent_fee = parent_fee * 10**8
    
    # # Get parent transaction size
    # parent_tx_hex = rpc_connection.getrawtransaction(txid)
    # parent_tx_dict = rpc_connection.decoderawtransaction(parent_tx_hex)
    # parent_vsize = parent_tx_dict.get("vsize")
    # plugin.log(f"[WHISKEY] Contents of parent_vsize: {parent_vsize}")
    # parent_fee_rate = parent_fee / parent_vsize  # sat/vB
    # plugin.log(f"[XRAY] Contents of parent_fee_rate: {parent_fee_rate}")
    # plugin.log(f"[YANKEE] Contents of parent_fee: {parent_fee}")

    # child_fee = calculate_child_fee(
    #     parent_fee,
    #     parent_vsize,
    #     first_child_vsize, 
    #     fee_rate
    # )

    recipient_amount = int(amount - emergency_refill_amount - first_child_fee * 10**8) # Subtract emergency channel
    plugin.log(f"[UNIFORM] amount: {amount},  Reserve amount: {emergency_refill_amount} sats, Recipient amount: {recipient_amount} sats, first_child_fee: {first_child_fee}")
    # plugin.log(f"[VICTOR] fee: {fee}")
    #     # First attempt using the bitcoin rpc_connection function:


    # fee_rate_after_bump = child_fee / first_child_vsize

    # # Ensure that fee_rate_after_bump is an integer or formatted with 'perkb'
    # feerate_integer = int(fee_rate_after_bump * 1000)  # Convert to an integer by multiplying by 1000
    # feerate_string = f"{feerate_integer}perkb"  # Add the 'perkb' unit to the integer


      # Use repr() to make the output more readable
    # plugin.log(f"[DEBUG] 1st feerate: {feerate_string}")


    # Generate change address for emergency reserve
    try:
        reserve_address = plugin.rpc.newaddr()['bech32']
        plugin.log(f"[RESERVE] Generated reserve address: {reserve_address}")
    except RpcError as e:
        raise CPFPError(f"Failed to generate reserve address: {str(e)}")


    # Second time we call txprepare
    try:
        plugin.log(f"[SECOND_TXPREPARE] calling txprepare with: {recipient_amount} {utxo_selector}")
        second_rpc_result = plugin.rpc.txprepare(
            outputs = [
                {address: int(recipient_amount)}, # Correct format for 'outputs'
                #{reserve_address: int(emergency_refill_amount)}
            ],
            utxos=utxo_selector,  # Ensure 'utxo_selector' is correctly formatted as a list of txid:vout strings
            #feerate=feerate_string
        )

        plugin.log(f"[RESERVE 2] recipient_amount: {recipient_amount}")
    

        # ### AQUI ME QUEDE DE HACER EL TRACING LINE POR LINEA
        

        plugin.log(f"[ZULU] second_rpc_result: {second_rpc_result}")
        plugin.log(f"[ALPHA-ALPHA] second_feerate: {fee_rate}")

        second_psbt_v2_str = second_rpc_result.get("psbt")

        second_v0_psbt = plugin.rpc.setpsbtversion(
            psbt=second_psbt_v2_str,
            version=0
        )
        plugin.log(f"[ALPHA-BRAVO] second_v0_psbt: {second_v0_psbt}")

        second_new_psbt= PartiallySignedTransaction.from_base64(second_v0_psbt.get("psbt"))

        second_fee = second_new_psbt.get_fee()
        plugin.log(f"[ALPHA-CHARLIE] psbt second_fee: {second_fee}")

    except CPFPError as e:
        plugin.log(f"[ALPHA-JULIET] CPFPError occurred: {str(e)}")
        raise CPFPError("Error creating CPFP transaction.")
    except RpcError as e:
        plugin.log(f"[ALPHA-KILO] RPC Error during withdrawal: {str(e)}")
        raise CPFPError(f"RPC Error while withdrawing funds: {str(e)}")
    except Exception as e:
        plugin.log(f"[ALPHA-LIMA] General error occurred while withdrawing: {str(e)}")
        raise CPFPError(f"Error while withdrawing funds: {str(e)}")





    try:
            second_signed_v2_psbt = plugin.rpc.signpsbt(
                psbt=second_psbt_v2_str
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
            second_child_fee_generated = second_child_analyzed["fee"]
            second_child_vsize = second_child_analyzed["estimated_vsize"]
            second_child_feerate = second_child_analyzed["estimated_feerate"]
            plugin.log(f"[ALPHA-ECHO] Contents of second_child_fee_generated: {second_child_fee_generated}")
            plugin.log(f"[ALPHA-FOXTROT] Contents of second_child_vsize: {second_child_vsize}")
            plugin.log(f"[ALPHA-GOLF] Contents of second_child_feerate: {second_child_feerate}")

    except CPFPError as e:
        plugin.log(f"[ALPHA-JULIET] CPFPError occurred: {str(e)}")
        raise CPFPError("Error creating CPFP transaction.")
    except RpcError as e:
        plugin.log(f"[ALPHA-KILO] RPC Error during withdrawal: {str(e)}")
        raise CPFPError(f"RPC Error while withdrawing funds: {str(e)}")
    except Exception as e:
        plugin.log(f"[ALPHA-LIMA] General error occurred while withdrawing: {str(e)}")
        raise CPFPError(f"Error while withdrawing funds: {str(e)}")


    plugin.rpc.unreserveinputs(
        psbt=second_rpc_result.get("psbt"),
    )


    # Step 9: Log and return the transaction details
    second_child = rpc_result.get("txid")
    second_psbt = rpc_result.get("psbt")
    second_signed_psbt = ""

    # txid contains a new txid
    plugin.log(f"[ALPHA-HOTEL] txid variable contains this txid: {txid}")
    plugin.log(f"[ALPHA-INDIA] second_child variable contains this txid: {second_child}")




    second_child_fee = calculate_child_fee(
        parent_fee,
        parent_vsize,
        second_child_vsize, 
        fee_rate
    )

    #incorrect feerate 4859perkb

    plugin.log(f"[THIRD_RECIPIENT_AMOUNT_CALC]: {amount} - {emergency_refill_amount} - {second_child_fee}")
    recipient_amount = int(amount - emergency_refill_amount - second_child_fee)
    plugin.log(f"[UNIFORM] Reserve amount: {emergency_refill_amount} sats, Recipient amount: {recipient_amount} sats")
    plugin.log(f"[VICTOR] second_child_fee: {second_child_fee}")
        # First attempt using the bitcoin rpc_connection function:

    fee_rate_after_bump = second_child_fee / second_child_vsize
    plugin.log(f"[FEE_RATE_AFTER_BUMP_CALC] {fee_rate_after_bump} = {second_child_fee} / {second_child_vsize}")

    # Ensure that fee_rate_after_bump is an integer or formatted with 'perkb'
    feerate_integer = int(fee_rate_after_bump * 1000)  # Convert to an integer by multiplying by 1000
    feerate_string = f"{feerate_integer}perkb"  # Add the 'perkb' unit to the integer


      # Use repr() to make the output more readable
    plugin.log(f"[DEBUG] utxos: {utxo_selector}")
    plugin.log(f"[DEBUG] 2nd feerate: {feerate_string}")


    # Third time we call txprepare
    try:
        plugin.log(f"[THIRD_TXPREPARE] calling txprepare with: {recipient_amount} {utxo_selector} {feerate_string}")
        third_rpc_result = plugin.rpc.txprepare(
            outputs = [{address: int(recipient_amount)}],  # Correct format for 'outputs'
            utxos=utxo_selector,  # Ensure 'utxo_selector' is correctly formatted as a list of txid:vout strings
            # feerate=feerate_string
            feerate="4536perkb"
            # feerate="4536perkb"
        )
    
        

        plugin.log(f"[ZULU] third_rpc_result: {third_rpc_result}")
        plugin.log(f"[ALPHA-ALPHA] third_feerate: {fee_rate}")

        third_v0_psbt = plugin.rpc.setpsbtversion(
            psbt=third_rpc_result.get("psbt"),
            version=0
        )
        plugin.log(f"[ALPHA-BRAVO] third_v0_psbt: {third_v0_psbt}")

        third_new_psbt= PartiallySignedTransaction.from_base64(third_v0_psbt.get("psbt"))

        third_fee = third_new_psbt.get_fee()
        plugin.log(f"[ALPHA-CHARLIE] psbt third_fee: {third_fee}")

    except CPFPError as e:
        plugin.log(f"[ALPHA-JULIET] CPFPError occurred: {str(e)}")
        raise CPFPError("Error creating CPFP transaction.")
    except RpcError as e:
        plugin.log(f"[ALPHA-KILO] RPC Error during withdrawal: {str(e)}")
        raise CPFPError(f"RPC Error while withdrawing funds: {str(e)}")
    except Exception as e:
        plugin.log(f"[ALPHA-LIMA] General error occurred while withdrawing: {str(e)}")
        raise CPFPError(f"Error while withdrawing funds: {str(e)}")





    # try:
    #         first_signed_v2_psbt = plugin.rpc.signpsbt(
    #             psbt=first_psbt
    #         )

    #         first_signed_v0_psbt = plugin.rpc.setpsbtversion(
    #             psbt=first_signed_v2_psbt.get("signed_psbt"),
    #             version=0
    #         )
    #         first_child_v0_psbt = first_signed_v0_psbt.get("psbt")
    #         first_psbt_v0 = "'" + first_child_v0_psbt + "'"
    #         first_psbt_v2 = first_signed_v2_psbt.get("signed_psbt")
    #         plugin.log(f"[ALPHA-WHISKEY] Contents of rpc_connection: {rpc_connection}")
    #         first_child_analyzed = rpc_connection.analyzepsbt(first_child_v0_psbt)
    #         first_child_fee = first_child_analyzed["fee"]
    #         first_child_vsize = first_child_analyzed["estimated_vsize"]
    #         first_child_feerate = first_child_analyzed["estimated_feerate"]
    #         plugin.log(f"[ALPHA-ECHO] Contents of first_child_fee: {first_child_fee}")
    #         plugin.log(f"[ALPHA-FOXTROT] Contents of first_child_vsize: {first_child_vsize}")
    #         plugin.log(f"[ALPHA-GOLF] Contents of first_child_feerate: {first_child_feerate}")

    # except CPFPError as e:
    #     plugin.log(f"[ALPHA-JULIET] CPFPError occurred: {str(e)}")
    #     raise CPFPError("Error creating CPFP transaction.")
    # except RpcError as e:
    #     plugin.log(f"[ALPHA-KILO] RPC Error during withdrawal: {str(e)}")
    #     raise CPFPError(f"RPC Error while withdrawing funds: {str(e)}")
    # except Exception as e:
    #     plugin.log(f"[ALPHA-LIMA] General error occurred while withdrawing: {str(e)}")
    #     raise CPFPError(f"Error while withdrawing funds: {str(e)}")

    third_child_txid = third_rpc_result.get("txid")
    third_psbt = third_rpc_result.get("psbt")
    third_signed_psbt = ""
    
    # txid contains a new txid    
    plugin.log(f"[ALPHA-HOTEL] txid variable contains this txid: {txid}")
    plugin.log(f"[ALPHA-INDIA] first_child variable contains this txid: {first_child}")

    try:
        third_signed_v2_psbt = plugin.rpc.signpsbt(
            psbt=third_psbt
        )

        third_signed_v0_psbt = plugin.rpc.setpsbtversion(
            psbt=third_signed_v2_psbt.get("signed_psbt"),
            version=0
        )
        third_child_v0_psbt = third_signed_v0_psbt.get("psbt")
        third_psbt_v0 = "'" + third_child_v0_psbt + "'"
        third_psbt_v2 = third_signed_v2_psbt.get("signed_psbt")
        plugin.log(f"[ALPHA-WHISKEY] Contents of rpc_connection: {rpc_connection}")
        third_child_analyzed = rpc_connection.analyzepsbt(third_child_v0_psbt)
        third_child_fee = third_child_analyzed["fee"]
        third_child_vsize = third_child_analyzed["estimated_vsize"]
        third_child_feerate = third_child_analyzed["estimated_feerate"]
        plugin.log(f"[ALPHA-MIKE] Contents of third_child_fee: {third_child_fee}")
        plugin.log(f"[ALPHA-NOVEMBER] Contents of third_child_vsize: {third_child_vsize}")
        plugin.log(f"[ALPHA-OSCAR] Contents of third_child_feerate: {third_child_feerate}")

        #plugin.rpc.unreserveinputs(
           #psbt=third_rpc_result.get("psbt"),
        #)

        # third_child_fee = calculate_child_fee(
        #     parent_fee,
        #     parent_vsize,
        #     third_child_vsize, 
        #     fee_rate
        # )
        
        third_child_fee_sat = third_child_fee * 10**8

        print(f"Child transaction fee: {third_child_fee_sat} satoshis")
        plugin.log(f"[ALPHA-PAPA] line 547: third_child_fee_sat variable contains: {third_child_fee_sat}")

        child_fee_rate = third_child_fee_sat / third_child_vsize  # sat/vB
        plugin.log(f"[ALPHA-QUEBEC] line 438: child_fee_rate variable contains: {child_fee_rate}")

        plugin.log(f"[ALPHA-ROMEO] third_rpc_result: {json.dumps(third_rpc_result, indent=4)}")  # Log the full result

        total_vsizes = parent_vsize + third_child_vsize
        plugin.log(f"[ALPHA-SIERRA] Contents of total_vsizes: {total_vsizes}")
        plugin.log(f"[ALPHA-SIERRA-B] Contents of parent_fee: {parent_fee}")
        plugin.log(f"[ALPHA-SIERRA-C] Contents of third_child_fee_sat: {third_child_fee_sat}")
        total_fees = (parent_fee + third_child_fee_sat)  # Convert fees to satoshis if in BTC
        plugin.log(f"[ALPHA-TANGO] Contents of total_fees: {total_fees}")
        total_feerate = total_fees / total_vsizes
        plugin.log(f"[ALPHA-UNIFORM] Contents of total_feerate: {total_feerate}")

        plugin.log(f"[ALPHA-VICTOR] Signed PSBT (v2): {third_signed_v2_psbt}")
        plugin.log(f"[ALPHA-WHISKEY] Signed PSBT (v0): {third_signed_v0_psbt}")
    except CPFPError as e:
        plugin.log(f"[ALPHA-JULIET] CPFPError occurred: {str(e)}")
        raise CPFPError("Error creating CPFP transaction.")
    except RpcError as e:
        plugin.log(f"[ALPHA-KILO] RPC Error during withdrawal: {str(e)}")
        raise CPFPError(f"RPC Error while withdrawing funds: {str(e)}")
    except Exception as e:
        plugin.log(f"[ALPHA-LIMA] General error occurred while withdrawing: {str(e)}")
        raise CPFPError(f"Error while withdrawing funds: {str(e)}")

    # Convert all values to JSON-serializable types
    response = {
        "message": "Please make sure to run bitcoin-cli finalizepsbt and analyzepsbt to verify "
        "the details before broadcasting the transaction",
        "finalize_command": f'bitcoin-cli finalizepsbt {third_psbt_v0}',
        "analyze_command": f'bitcoin-cli analyzepsbt {third_psbt_v0}',
        "signed_psbt": str(third_psbt_v2),  # Convert to string
        "parent_fee": int(parent_fee),  # Add parent fee
        "parent_vsize": int(parent_vsize),  # Add parent vsize
        "parent_feerate": float(parent_fee_rate),  # Convert to float
        "third_child_fee_sat": int(third_child_fee_sat),  # Add child fee
        "child_vsize": int(third_child_vsize),  # Add child vsize
        "child_feerate": float(child_fee_rate),  # Convert to float
        "total_fees": int(total_fees),  # Convert to int
        "total_vsizes": int(total_vsizes),  # Convert to int
        "total_feerate": float(total_feerate),  # Convert to float
        "parent_psbt": plugin.rpc.setpsbtversion(psbt=first_psbt, version=0)['psbt'],
        "child_psbt": third_child_v0_psbt,
        "parent_txid": txid,
        "child_txid": third_child_txid
    }

    plugin.log(f"[BRAVO-ALPHA] line 556: txid variable contains this txid: {txid}")
    plugin.log(f"[BRAVO-BRAVO] line 557: third_child_txid variable contains this txid: {third_child_txid}")

    return response

plugin.run()
