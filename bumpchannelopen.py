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
    amount = amount_msat / 100_000_000_000  # Convert msat to BTC
    plugin.log(f"[DEBUG] Amount in sats: {amount} sats")

    # Get all addresses associated with the node
    listaddresses_result = plugin.rpc.listaddresses()

    # Debug log to inspect the returned addresses
    plugin.log(f"[DEBUG] listaddresses result: {listaddresses_result}")

    # Extract bech32 and p2tr addresses from the result
    valid_addresses = [
        entry[key] for entry in listaddresses_result.get("addresses", [])
        for key in ("bech32", "p2tr") if key in entry
    ]

    # Verify that the recipient address is in the list
    if address not in valid_addresses:
        plugin.log(f"[ERROR] Address {address} is not owned by this node.", level="error")
        return {"error": f"Recipient address {address} is not owned by this node"}

    plugin.log(f"[INFO] Address {address} is valid and owned by this node.")

    # Step 6: Use bitcoin rpc call `createpsbt` to create the partially signed bitcoin transaction and get the vsize
    utxo_selector = [{"txid": selected_utxo["txid"], "vout": selected_utxo["output"]}]
    plugin.log(f"[MIKE] Bumping selected output using UTXO {utxo_selector}")

    try:
        # Connect to bitcoin-cli
        rpc_connection = connect_bitcoincli(
            rpc_user=plugin.get_option('bump_brpc_user'),
            rpc_password=plugin.get_option('bump_brpc_pass'),
            port=plugin.get_option('bump_brpc_port')
        )

        plugin.log(f"[ALPHA-WHISKEY] Contents of rpc_connection: {rpc_connection}")

        # Create PSBT
        rpc_result = rpc_connection.createpsbt(
            utxo_selector,  # List of dictionaries
            [{address: amount}]  # Outputs as list of dictionaries
        )
        plugin.log(f"[NOVEMBER] Contents of rpc_result: {rpc_result}")

        # Load PSBT into python-bitcoinlib
        new_psbt = PartiallySignedTransaction.from_base64(rpc_result)
        plugin.log(f"[QUEBEC] Contents of new_psbt: {new_psbt}")

        # Update PSBT with missing UTXO data
        updated_psbt = rpc_connection.utxoupdatepsbt(rpc_result)
        plugin.log(f"[DELTA] Updated PSBT: {updated_psbt}")

        # Analyze PSBT after updating it
        first_child_analyzed = rpc_connection.analyzepsbt(updated_psbt)
        plugin.log(f"[ALPHA-HOTEL0.5] first_child_analyzed variable contains: {first_child_analyzed}")

        # Step 9: Log and return the transaction details
        first_psbt = updated_psbt  # The latest PSBT in base64 format
        first_child_vsize = first_child_analyzed.get("estimated_vsize")
        first_child_feerate = first_child_analyzed.get("estimated_feerate")
        first_child_fee = first_child_analyzed.get("fee")

        plugin.log(f"[TRANSACTION DETAILS] PSBT: {first_psbt}")
        plugin.log(f"[TRANSACTION DETAILS] Estimated vsize: {first_child_vsize}")
        plugin.log(f"[TRANSACTION DETAILS] Estimated feerate: {first_child_feerate}")
        plugin.log(f"[TRANSACTION DETAILS] Estimated fee: {first_child_fee}")

    except CPFPError as e:
        plugin.log(f"[ROMEO] CPFPError occurred: {str(e)}")
        raise CPFPError("Error creating CPFP transaction.")
    except RpcError as e:
        plugin.log(f"[SIERRA] RPC Error during withdrawal: {str(e)}")
        raise RpcError(f"RPC Error while withdrawing funds: {str(e)}")
    except Exception as e:
        plugin.log(f"[TANGO] General error occurred: {str(e)}")
        raise Exception(f"Error occurred: {str(e)}")

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

    # Calculate the child's fee
    desired_child_fee = calculate_child_fee(
        parent_fee,
        parent_vsize,
        first_child_vsize, 
        fee_rate
    )

    plugin.log(f"[YANKEE1.5] Contents of desired_child_fee: {desired_child_fee}")

    recipient_amount = amount - (float(desired_child_fee) / 10**8) # Subtract manually estimated fees, all should be in BTC
    plugin.log(f"[UNIFORM] amount: {amount}, Recipient amount: {recipient_amount}, first_child_fee: {desired_child_fee}")

    # Step 6: Use bitcoin rpc call `createpsbt` a second time using the amount - the child's_fee

    try:
        # Connect to bitcoin-cli
        rpc_connection = connect_bitcoincli(
            rpc_user=plugin.get_option('bump_brpc_user'),
            rpc_password=plugin.get_option('bump_brpc_pass'),
            port=plugin.get_option('bump_brpc_port')
        )

        plugin.log(f"[ALPHA-WHISKEY] Contents of rpc_connection: {rpc_connection}")

        # Create PSBT
        rpc_result2 = rpc_connection.createpsbt(
            utxo_selector,  # List of dictionaries
            [{address: recipient_amount}]  # Outputs as list of dictionaries
        )
        plugin.log(f"[NOVEMBER] Contents of rpc_result2: {rpc_result2}")

        # Load PSBT into python-bitcoinlib
        new_psbt2 = PartiallySignedTransaction.from_base64(rpc_result2)
        plugin.log(f"[QUEBEC] Contents of new_psbt2: {new_psbt2}")

        # Update PSBT with missing UTXO data
        updated_psbt2 = rpc_connection.utxoupdatepsbt(rpc_result2)
        plugin.log(f"[DELTA] Updated PSBT2: {updated_psbt2}")

        # Analyze PSBT after updating it
        second_child_analyzed = rpc_connection.analyzepsbt(updated_psbt2)
        plugin.log(f"[ALPHA-HOTEL0.5] second_child_analyzed variable contains: {second_child_analyzed}")

        # Step 9: Log and return the transaction details
        second_psbt = updated_psbt2  # The latest PSBT in base64 format
        second_child_vsize = second_child_analyzed.get("estimated_vsize")
        second_child_feerate = second_child_analyzed.get("estimated_feerate")
        second_child_fee = second_child_analyzed.get("fee")

        plugin.log(f"[TRANSACTION DETAILS] PSBT: {second_psbt}")
        plugin.log(f"[TRANSACTION DETAILS] Estimated vsize: {second_child_vsize}")
        plugin.log(f"[TRANSACTION DETAILS] Estimated feerate: {second_child_feerate}")
        plugin.log(f"[TRANSACTION DETAILS] Estimated fee: {second_child_fee}")

    except CPFPError as e:
        plugin.log(f"[ROMEO] CPFPError occurred: {str(e)}")
        raise CPFPError("Error creating CPFP transaction.")
    except RpcError as e:
        plugin.log(f"[SIERRA] RPC Error during withdrawal: {str(e)}")
        raise RpcError(f"RPC Error while withdrawing funds: {str(e)}")
    except Exception as e:
        plugin.log(f"[TANGO] General error occurred: {str(e)}")
        raise Exception(f"Error occurred: {str(e)}")

    try:

        # Connect to bitcoin-cli
        rpc_connection = connect_bitcoincli(
            rpc_user=plugin.get_option('bump_brpc_user'),
            rpc_password=plugin.get_option('bump_brpc_pass'),
            port=plugin.get_option('bump_brpc_port')
        )

        # Reserve the UTXO before signing
        plugin.rpc.reserveinputs(psbt=second_psbt)

        # Sign the PSBT
        second_signed_psbt = plugin.rpc.signpsbt(psbt=second_psbt)

        # Extract the signed PSBT
        second_child_psbt = second_signed_psbt.get("signed_psbt")

        if not second_child_psbt:
            raise CPFPError("Signing failed. No signed PSBT returned.")

        plugin.log(f"[DEBUG] Signed PSBT: {second_child_psbt}")

        # Finalize the PSBT but do NOT extract yet
        finalized_psbt = rpc_connection.finalizepsbt(second_child_psbt, False)
        plugin.log(f"[DEBUG] finalized_psbt: {finalized_psbt}")

        finalized_psbt_base64 = finalized_psbt.get("psbt")
        if not finalized_psbt_base64:
            raise CPFPError("PSBT was not properly finalized. No PSBT hex returned.")

        # Log the raw PSBT for inspection
        plugin.log(f"[DEBUG] Finalized PSBT (base64: {finalized_psbt_base64}")

        # Decode the finalized PSBT
        signed_child_decoded = rpc_connection.decodepsbt(finalized_psbt_base64)
        plugin.log(f"[DEBUG] signed_child_decoded after finalization: {signed_child_decoded}")

        signed_child_fee = signed_child_decoded.get("fee")

        try:
            feerate_satvbyte = (float(signed_child_fee) * 1e8) / int(second_child_vsize)
        except (TypeError, ValueError, ZeroDivisionError) as e:
            plugin.log(f"[ERROR] Failed to compute feerate: {str(e)}")

        plugin.log(f"[ALPHA-ECHO] Contents of signed_child_fee: {signed_child_fee}")
        plugin.log(f"[ALPHA-FOXTROT] Contents of signed_child_vsize: {second_child_vsize}")
        plugin.log(f"[ALPHA-GOLF] Contents of signed_child_feerate: {feerate_satvbyte}")

        # Extract raw final transaction
        fully_finalized = rpc_connection.finalizepsbt(finalized_psbt_base64, True)
        final_tx_hex = fully_finalized.get("hex")
        if not final_tx_hex:
            raise CPFPError("Could not extract hex from finalized PSBT.")

        # Decode raw transaction
        decoded_tx = rpc_connection.decoderawtransaction(final_tx_hex)
        actual_vsize = decoded_tx.get("vsize")
        plugin.log(f"[ALPHA-HOTEL] Actual vsize: {actual_vsize}")

        txid = decoded_tx.get("txid")
        plugin.log(f"[ALPHA-INDIA] Final transaction ID (txid): {txid}")

    except CPFPError as e:
        plugin.log(f"[ALPHA-JULIET] CPFPError occurred: {str(e)}")
        raise CPFPError("Error creating CPFP transaction.")
    except RpcError as e:
        plugin.log(f"[ALPHA-KILO] RPC Error during withdrawal: {str(e)}")
        raise CPFPError(f"RPC Error while withdrawing funds: {str(e)}")
    except Exception as e:
        plugin.log(f"[ALPHA-LIMA] General error occurred while withdrawing: {str(e)}")
        raise CPFPError(f"Error while withdrawing funds: {str(e)}")

    # Unreserve inputs, just for testing!
    # plugin.rpc.unreserveinputs(psbt=rpc_result2)

    # Convert child_fee to satoshis
    child_fee_satoshis = float(signed_child_fee) * 100000000  # Convert to satoshis

    # Calculate total fees (sum of parent and child fees)
    total_fees = int(parent_fee) + int(child_fee_satoshis)

    # Calculate total vsize (sum of parent and child vsizes)
    total_vsizes = int(parent_vsize) + int(second_child_vsize)

    # Calculate the total fee rate (total_fees / total_vsizes)
    total_feerate = total_fees / total_vsizes  # This will give the fee rate in sat/vbyte

    # Prepare the response
    response = {
        "message": "This is beta software, this might spend all your money. Please make sure to run bitcoin-cli analyzepsbt to verify "
                "the fee before broadcasting the transaction",
        "analyze_command": f'bitcoin-cli analyzepsbt {finalized_psbt_base64}',  # Signed and finalized PSBT

        # "child_txid": txid,  # Child transaction ID

        "parent_fee": int(parent_fee),  # Parent fee value from logs
        "parent_vsize": int(parent_vsize),  # Parent vsize value from logs
        "parent_feerate": float(parent_fee_rate),  # Parent fee rate value from logs

        "child_fee": int(float(signed_child_fee) * 10**8),  # Convert BTC to sats
        "child_vsize": int(second_child_vsize),  # Child vsize value
        "child_feerate": float(feerate_satvbyte),  # Child fee rate

        # Calculate total fees and feerates correctly
        "total_fees": total_fees,  # Total fee value in satoshis
        "total_vsizes": total_vsizes,  # Total vsize
        "total_feerate": total_feerate,  # Correct total feerate

        "desired_total_feerate": fee_rate,  # Desired total fee rate

        "message2": "Run sendrawtransaction to broadcast your cpfp transaction",
        "sendrawtransaction_command": f'bitcoin-cli sendrawtransaction {final_tx_hex}',
    }

    return response

plugin.run()
