#!/usr/bin/env python3
from pyln.client import Plugin, RpcError
import json
from bitcointx.core.psbt import PartiallySignedTransaction
from bitcoinrpc.authproxy import AuthServiceProxy, JSONRPCException
import os
import sys

plugin = Plugin()

class CPFPError(Exception):
    """Custom exception for CPFP-related errors"""
    pass

# Plugin configuration options
plugin.add_option('bump_brpc_user', None, 'bitcoin rpc user')
plugin.add_option('bump_brpc_pass', None, 'bitcoin rpc password')
plugin.add_option('bump_brpc_port', 18443, 'bitcoin rpc port')
plugin.add_option(
    "yolo",
    None,
    "Set to 'yolo' to broadcast transaction automatically after finalizing the psbt"
)

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
    try:
        parent_fee = float(parent_fee)
        desired_total_feerate = float(desired_total_feerate)
        total_vsize = parent_vsize + child_vsize
        required_total_fee = desired_total_feerate * total_vsize
        child_fee = required_total_fee - parent_fee
        return child_fee
    except (TypeError, ValueError) as e:
        raise CPFPError("Invalid fee calculation: incompatible number types") from e

def wrap_method(func):
    """
    Wraps a plugin method to catch TypeError from argument validation and return clean JSON-RPC errors.
    """
    def wrapper(plugin, *args, **kwargs):
        try:
            return func(plugin, *args, **kwargs)
        except TypeError as e:
            plugin.log(f"[ERROR] Invalid arguments: {str(e)}")
            return {
                "code": -32600,
                "message": "Missing required argument: ensure txid, vout, and fee_rate are provided"
            }
        except Exception as e:
            plugin.log(f"[ERROR] Unexpected error: {str(e)}")
            return {
                "code": -32600,
                "message": f"Unexpected error: {str(e)}"
            }
    return wrapper

@plugin.method("bumpchannelopen",
               desc="Creates a CPFP transaction to bump the feerate of a parent output, with checks for emergency reserve.",
               long_desc="Creates a Child-Pays-For-Parent (CPFP) transaction to increase the feerate of a specified output. "
                         "Use `listfunds` to check unreserved funds before bumping. Use `yolo` mode to broadcast transaction automatically")
@wrap_method
def bumpchannelopen(plugin, txid, vout, fee_rate, yolo=None):
    """
    Creates a CPFP transaction for a specific parent output.

    Args:
        txid: Parent transaction ID (string)
        vout: Output index (non-negative integer)
        fee_rate: Desired fee rate in sat/vB (number)
        yolo: Set to 'yolo' to send transaction automatically
    """
    # Input validation
    if not isinstance(txid, str) or not txid:
        return {"code": -32600, "message": "Invalid or missing txid: must be a non-empty string"}
    if not isinstance(vout, int) or vout < 0:
        return {"code": -32600, "message": "Invalid vout: must be a non-negative integer"}
    try:
        float(fee_rate)
    except (TypeError, ValueError):
        return {"code": -32600, "message": "Invalid fee_rate: must be a number"}

    if yolo == "yolo":
        plugin.log("YOLO mode is ON!")
    else:
        plugin.log("Safety mode is ON!")

    # Step 1: Get new address
    new_addr = plugin.rpc.newaddr()
    address = new_addr.get('bech32')
    plugin.log(f"[BRAVO] Input Parameters - txid: {txid}, vout: {vout}, fee_rate: {fee_rate}")
    plugin.log(f"[BRAVO2.0] Got new bech32 address from node: address: {address}")

    # Step 2: Fetch network information
    try:
        info = plugin.rpc.getinfo()
        network = info.get('network')
        plugin.log(f"[CHARLIE] Network detected: {network}")
        if not network:
            return {"code": -32600, "message": "Network information is missing"}
    except RpcError as e:
        plugin.log(f"[SIERRA] RPC Error: {str(e)}")
        return {"code": -32600, "message": f"Failed to fetch network info: {str(e)}"}

    # Step 3: Get list of UTXOs
    try:
        funds = plugin.rpc.listfunds()
        plugin.log(f"[INFO] Funds retrieved: {funds}")
        utxos = funds.get("outputs", [])
        if not utxos:
            return {"code": -32600, "message": "No unspent transaction outputs found"}
    except RpcError as e:
        plugin.log(f"[SIERRA] RPC Error: {str(e)}")
        return {"code": -32600, "message": f"Failed to fetch funds: {str(e)}"}

    plugin.log("[DEBUG] All UTXOs before filtering:")
    for idx, utxo in enumerate(utxos):
        reserved_status = utxo.get("reserved", False)
        plugin.log(f"[DEBUG] UTXO {idx}: txid={utxo['txid']} vout={utxo['output']} amount={utxo['amount_msat']} msat, reserved={reserved_status}")

    available_utxos = [utxo for utxo in utxos if not utxo.get("reserved", False)]
    plugin.log("[INFO] Available UTXOs after filtering:")
    if not available_utxos:
        plugin.log("[ECHO] No unreserved UTXOs available")
        return {"code": -32600, "message": "No unreserved unspent transaction outputs found"}

    for idx, utxo in enumerate(available_utxos):
        plugin.log(f"[FOXTROT] {idx}: txid={utxo['txid']} vout={utxo['output']} amount={utxo['amount_msat']} msat")

    plugin.log(f"[DEBUG] Count of available UTXOs: {len(available_utxos)}")
    if available_utxos:
        plugin.log(f"[DEBUG] Available UTXOs contents: {available_utxos}")

    # Select UTXO
    selected_utxo = None
    for utxo in available_utxos:
        if utxo["txid"] == txid and utxo["output"] == vout:
            selected_utxo = utxo
            break

    if not selected_utxo:
        return {"code": -32600, "message": f"UTXO {txid}:{vout} not found in available UTXOs"}

    plugin.log(f"[DEBUG] Selected UTXO: txid={selected_utxo['txid']}, vout={selected_utxo['output']}, amount={selected_utxo['amount_msat']} msat")

    # Step 4: Calculate parent transaction details
    try:
        rpc_connection = connect_bitcoincli(
            rpc_user=plugin.get_option('bump_brpc_user'),
            rpc_password=plugin.get_option('bump_brpc_pass'),
            port=plugin.get_option('bump_brpc_port')
        )
        tx = rpc_connection.getrawtransaction(txid, True)
        plugin.log(f"[TANGO - WHISKEY] Contents tx: {tx}")

        total_inputs = 0
        for vin in tx["vin"]:
            input_tx = rpc_connection.getrawtransaction(vin["txid"], True)
            total_inputs += input_tx["vout"][vin["vout"]]["value"]
        plugin.log(f"[TANGO - WHISKEY 2] Contents of total_inputs: {total_inputs}")

        total_outputs = sum(vout["value"] for vout in tx["vout"])
        plugin.log(f"[TANGO - WHISKEY 3] Contents of total_outputs: {total_outputs}")

        parent_fee = total_inputs - total_outputs
        parent_fee = parent_fee * 10**8
        plugin.log(f"[TANGO - WHISKEY 4] Contents of parent_fee: {parent_fee}")

        parent_tx_hex = rpc_connection.getrawtransaction(txid)
        parent_tx_dict = rpc_connection.decoderawtransaction(parent_tx_hex)
        parent_vsize = parent_tx_dict.get("vsize")
        plugin.log(f"[WHISKEY] Contents of parent_vsize: {parent_vsize}")

        parent_fee_rate = parent_fee / parent_vsize  # sat/vB
        plugin.log(f"[YANKEE] Contents of parent_fee_rate: {parent_fee_rate}")
    except JSONRPCException as e:
        plugin.log(f"[SIERRA] RPC Error: {str(e)}")
        return {"code": -32600, "message": f"Failed to fetch transaction: {str(e)}"}

    # Step 5: Check if transaction is confirmed
    if tx.get("confirmations", 0) > 0:
        return {"code": -32600, "message": "Transaction is already confirmed and cannot be bumped"}

    # Step 6: Fetch UTXO details
    amount_msat = selected_utxo["amount_msat"]
    if not amount_msat:
        return {"code": -32600, "message": f"UTXO {txid}:{vout} not found or already spent"}

    amount = amount_msat / 100_000_000_000
    plugin.log(f"[DEBUG] Amount in BTC: {amount}")

    # Step 7: Verify address
    try:
        listaddresses_result = plugin.rpc.listaddresses()
        valid_addresses = [
            entry[key] for entry in listaddresses_result.get("addresses", [])
            for key in ("bech32", "p2tr") if key in entry
        ]
        if address not in valid_addresses:
            plugin.log(f"[ERROR] Address {address} is not owned by this node", level="error")
            return {"code": -32600, "message": f"Recipient address {address} is not owned by this node"}
    except RpcError as e:
        plugin.log(f"[SIERRA] RPC Error: {str(e)}")
        return {"code": -32600, "message": f"Failed to verify address: {str(e)}"}

    plugin.log(f"[INFO] Address {address} is valid and owned by this node")

    # Step 8: Create first PSBT
    utxo_selector = [{"txid": selected_utxo["txid"], "vout": selected_utxo["output"]}]
    plugin.log(f"[MIKE] Bumping selected output using UTXO {utxo_selector}")
    try:
        rpc_result = rpc_connection.createpsbt(utxo_selector, [{address: amount}])
        plugin.log(f"[DEBUG] Contents of PSBT: {rpc_result}")
        updated_psbt = rpc_connection.utxoupdatepsbt(rpc_result)
        plugin.log(f"[DEBUG] Updated PSBT: {updated_psbt}")
        first_child_analyzed = rpc_connection.analyzepsbt(updated_psbt)
        plugin.log(f"[DEBUG] First child analyzed: {first_child_analyzed}")

        first_psbt = updated_psbt
        first_child_vsize = first_child_analyzed.get("estimated_vsize")
        first_child_feerate = first_child_analyzed.get("estimated_feerate")
        first_child_fee = first_child_analyzed.get("fee")
        plugin.log(f"[TRANSACTION DETAILS] PSBT: {first_psbt}")
        plugin.log(f"[TRANSACTION_DETAILS] Estimated vsize: {first_child_vsize}")
        plugin.log(f"[TRANSACTION_DETAILS] Estimated fee rate: {first_child_feerate}")
        plugin.log(f"[TRANSACTION_DETAILS] Estimated fee: {first_child_fee}")
    except (JSONRPCException, RpcError) as e:
        plugin.log(f"[SIERRA] RPC Error during PSBT creation: {str(e)}")
        return {"code": -32600, "message": f"Failed to create PSBT: {str(e)}"}
    except Exception as e:
        plugin.log(f"[ROMEO] Error during PSBT creation: {str(e)}")
        return {"code": -32600, "message": f"Unexpected error during PSBT creation: {str(e)}"}

    # Step 9: Calculate child fee and check emergency reserve
    target_feerate = float(fee_rate)  # Validation already done
    total_unreserved_sats = sum(utxo["amount_msat"] // 1000 for utxo in available_utxos)
    child_fee = 0
    if parent_fee_rate < target_feerate:
        child_vsize = first_child_vsize
        desired_total_fee = target_feerate * (parent_vsize + child_vsize)
        child_fee = max(0, float(desired_total_fee) - float(parent_fee))  # Convert to float
    plugin.log(f"[DEBUG] Total unreserved balance: {total_unreserved_sats} sats, estimated child fee: {child_fee} sats")

    if total_unreserved_sats - child_fee < 25000:
        plugin.log(f"[WARNING] Bump would leave {total_unreserved_sats - child_fee} sats, below 25000 sat emergency reserve.")
        return {
            "code": -32600,
            "message": f"Bump would leave {total_unreserved_sats - child_fee} sats, below 25000 sat emergency reserve.",
            "child_fee": child_fee
        }

    # Step 10: Check feerate
    if parent_fee_rate >= target_feerate:
        plugin.log(f"[INFO] Skipping PSBT: parent fee rate {parent_fee_rate:.2f} sat/vB "
                   f"meets or exceeds target {target_feerate:.2f} sat/vB")
        return {
            "message": "No CPFP needed: parent fee rate exceeds target",
            "parent_fee": int(parent_fee),
            "parent_vsize": int(parent_vsize),
            "parent_feerate": float(parent_fee_rate),
            "child_fee": 0,
            "child_vsize": 0,
            "child_feerate": 0,
            "total_fees": int(parent_fee),
            "total_vsizes": int(parent_vsize),
            "total_feerate": float(parent_fee_rate),
            "desired_total_feerate": target_feerate
        }

    # Step 11: Calculate confirmed unreserved amount
    total_sats = calculate_confirmed_unreserved_amount(funds, txid, vout)
    plugin.log(f"[GOLF] Total amount in confirmed and unreserved outputs: {total_sats} sats")

    # Step 12: Calculate child fee
    try:
        desired_child_fee = calculate_child_fee(parent_fee, parent_vsize, first_child_vsize, fee_rate)
        plugin.log(f"[YANKEE1.5] Contents of desired_child_fee: {desired_child_fee}")
    except CPFPError as e:
        plugin.log(f"[ROMEO] CPFPError occurred: {str(e)}")
        return {"code": -32600, "message": f"Failed to calculate child fee: {str(e)}"}

    amount = format(amount, '.8f')
    recipient_amount = float(amount) - (float(desired_child_fee) / 10**8)
    recipient_amount = format(recipient_amount, '.8f')
    plugin.log(f"[UNIFORM] amount: {amount}, Recipient amount: {recipient_amount}, first_child_fee: {desired_child_fee}")

    # Step 13: Check minimum relay fee
    MIN_RELAY_FEE = 1.0
    child_feerate = desired_child_fee / first_child_vsize
    if child_feerate < MIN_RELAY_FEE:
        return {
            "code": -32600,
            "message": f"Child transaction feerate ({child_feerate:.2f} sat/vB) below minimum relay fee ({MIN_RELAY_FEE} sat/vB). Increase fee_rate."
        }

    # Step 14: Create second PSBT
    try:
        rpc_result2 = rpc_connection.createpsbt(utxo_selector, [{address: recipient_amount}])
        plugin.log(f"[DEBUG] Contents of second PSBT: {rpc_result2}")
        new_psbt2 = PartiallySignedTransaction.from_base64(rpc_result2)
        plugin.log(f"[DEBUG] Contents of new_psbt2: {new_psbt2}")
        updated_psbt2 = rpc_connection.utxoupdatepsbt(rpc_result2)
        plugin.log(f"[DEBUG] Updated PSBT2: {updated_psbt2}")
        second_child_analyzed = rpc_connection.analyzepsbt(updated_psbt2)
        plugin.log(f"[DEBUG] Second child analyzed: {second_child_analyzed}")

        second_psbt = updated_psbt2
        second_child_vsize = second_child_analyzed.get("estimated_vsize")
        second_child_feerate = second_child_analyzed.get("estimated_feerate")
        second_child_fee = second_child_analyzed.get("fee")
        plugin.log(f"[TRANSACTION_DETAILS] PSBT: {second_psbt}")
        plugin.log(f"[TRANSACTION_DETAILS] Estimated vsize: {second_child_vsize}")
        plugin.log(f"[TRANSACTION_DETAILS] Estimated fee rate: {second_child_feerate}")
        plugin.log(f"[TRANSACTION_DETAILS] Estimated fee: {second_child_fee}")
    except (JSONRPCException, RpcError) as e:
        plugin.log(f"[SIERRA] RPC Error during PSBT creation: {str(e)}")
        return {"code": -32600, "message": f"Failed to create second PSBT: {str(e)}"}
    except Exception as e:
        plugin.log(f"[ROMEO] Error during PSBT creation: {str(e)}")
        return {"code": -32600, "message": f"Unexpected error during second PSBT creation: {str(e)}"}

    # Step 15: Reserve and sign PSBT
    try:
        plugin.rpc.reserveinputs(psbt=second_psbt)
        second_signed_psbt = plugin.rpc.signpsbt(psbt=second_psbt)
        plugin.log(f"[DEBUG] signpsbt response: {second_signed_psbt}")
        second_child_psbt = second_signed_psbt.get("signed_psbt", second_signed_psbt.get("psbt"))
        if not second_child_psbt:
            return {"code": -32600, "message": "Signing failed. No signed PSBT returned."}
        plugin.log(f"[DEBUG] Signed PSBT: {second_child_psbt}")

        finalized_psbt = rpc_connection.finalizepsbt(second_child_psbt, False)
        plugin.log(f"[DEBUG] finalized_psbt: {finalized_psbt}")
        finalized_psbt_base64 = finalized_psbt.get("psbt")
        if not finalized_psbt_base64:
            return {"code": -32600, "message": "PSBT was not properly finalized. No PSBT hex returned."}
    except (JSONRPCException, RpcError) as e:
        plugin.log(f"[SIERRA] RPC Error during PSBT signing: {str(e)}")
        return {"code": -32600, "message": f"Failed to reserve or sign PSBT: {str(e)}"}
    except Exception as e:
        plugin.log(f"[ROMEO] Error during PSBT signing: {str(e)}")
        return {"code": -32600, "message": f"Unexpected error during PSBT signing: {str(e)}"}

    # Step 16: Analyze final transaction
    try:
        signed_child_decoded = rpc_connection.decodepsbt(finalized_psbt_base64)
        plugin.log(f"[DEBUG] signed_child_decoded after finalization: {signed_child_decoded}")
        signed_child_fee = signed_child_decoded.get("fee")

        try:
            feerate_satvbyte = (float(signed_child_fee) * 1e8) / int(second_child_vsize)
        except (TypeError, ValueError, ZeroDivisionError) as e:
            plugin.log(f"[ERROR] Failed to compute feerate: {str(e)}")
            feerate_satvbyte = 0

        plugin.log(f"[DEBUG] Contents of signed_child_fee: {signed_child_fee}")
        plugin.log(f"[DEBUG] Contents of signed_child_vsize: {second_child_vsize}")
        plugin.log(f"[DEBUG] Contents of signed_child_feerate: {feerate_satvbyte}")

        fully_finalized = rpc_connection.finalizepsbt(finalized_psbt_base64, True)
        final_tx_hex = fully_finalized.get("hex")
        if not final_tx_hex:
            return {"code": -32600, "message": "Could not extract hex from finalized PSBT."}

        decoded_tx = rpc_connection.decoderawtransaction(final_tx_hex)
        actual_vsize = decoded_tx.get("vsize")
        plugin.log(f"[DEBUG] Actual vsize: {actual_vsize}")

        txid = decoded_tx.get("txid")
        plugin.log(f"[DEBUG] Final transaction ID (txid): {txid}")
    except (JSONRPCException, RpcError) as e:
        plugin.log(f"[SIERRA] RPC Error during transaction analysis: {str(e)}")
        return {"code": -32600, "message": f"Failed to analyze transaction: {str(e)}"}
    except Exception as e:
        plugin.log(f"[ROMEO] Error during transaction analysis: {str(e)}")
        return {"code": -32600, "message": f"Unexpected error during transaction analysis: {str(e)}"}

    # Step 17: Calculate totals
    child_fee_satoshis = float(signed_child_fee) * 100000000
    total_fees = int(parent_fee) + int(child_fee_satoshis)
    total_vsizes = int(parent_vsize) + int(second_child_vsize)
    total_feerate = total_fees / total_vsizes

    # Step 18: Build response
    response = {
        "message": "This is beta software, this might spend all your money. Please make sure to run bitcoin-cli analyzepsbt to verify "
                   "the fee before broadcasting the transaction",
        "analyze_command": f"bitcoin-cli analyzepsbt {finalized_psbt_base64}",
        "parent_fee": int(parent_fee),
        "parent_vsize": int(parent_vsize),
        "parent_feerate": float(parent_fee_rate),
        "child_fee": int(child_fee_satoshis),
        "child_vsize": int(second_child_vsize),
        "child_feerate": float(feerate_satvbyte),
        "total_fees": total_fees,
        "total_vsizes": total_vsizes,
        "total_feerate": total_feerate,
        "desired_total_feerate": float(fee_rate),
        "message2": "Run sendrawtransaction to broadcast your cpfp transaction",
        "sendrawtransaction_command": f"bitcoin-cli sendrawtransaction {final_tx_hex}"
    }

    # Step 19: Handle yolo mode
    if yolo is not None:
        if yolo == "yolo":
            try:
                plugin.log(f"[YOLO] Sending raw transaction...")
                sent_txid = rpc_connection.sendrawtransaction(final_tx_hex)
                plugin.log(f"[YOLO] Transaction sent! TXID: {sent_txid}")
                response = {
                    "message": "You used YOLO mode! Transaction sent! Please run the analyze and getrawtransaction commands to confirm transaction details.",
                    "analyze_command": f"bitcoin-cli analyzepsbt {finalized_psbt_base64}",
                    "getrawtransaction_command": f"bitcoin-cli getrawtransaction {sent_txid}",
                    "parent_fee": int(parent_fee),
                    "parent_vsize": int(parent_vsize),
                    "parent_feerate": float(parent_fee_rate),
                    "child_fee": int(child_fee_satoshis),
                    "child_vsize": int(second_child_vsize),
                    "child_feerate": float(feerate_satvbyte),
                    "total_fees": total_fees,
                    "total_vsizes": total_vsizes,
                    "total_feerate": total_feerate,
                    "desired_total_feerate": float(fee_rate)
                }
            except (JSONRPCException, RpcError) as e:
                plugin.log(f"[SIERRA] RPC Error during transaction broadcast: {str(e)}")
                return {"code": -32600, "message": f"Failed to broadcast transaction: {str(e)}"}
            except Exception as e:
                plugin.log(f"[ERROR] Error during transaction broadcast: {str(e)}")
                return {"code": -32600, "message": f"Unexpected error during transaction broadcast: {str(e)}"}
        else:
            try:
                plugin.rpc.unreserveinputs(psbt=finalized_psbt_base64)
            except (JSONRPCException, RpcError) as e:
                plugin.log(f"[SIERRA] RPC Error during unreserve: {str(e)}")
                return {"code": -32600, "message": f"Failed to unreserve inputs: {str(e)}"}
            response = {
                "message": "You missed YOLO mode! You passed an argument, but not `yolo`. Transaction created but not sent. Type the word `yolo` after the address or use `-k` with `yolo=yolo` to broadcast. "
                           "If you want to manually broadcast the created transaction please make sure to run bitcoin-cli analyzepsbt to verify the fee "
                           "and run bitcoin-cli sendrawtransction to broadcast it.",
                "analyze_command": f"bitcoin-cli analyzepsbt {finalized_psbt_base64}",
                "parent_fee": int(parent_fee),
                "parent_vsize": int(parent_vsize),
                "parent_feerate": float(parent_fee_rate),
                "child_fee": int(child_fee_satoshis),
                "child_vsize": int(second_child_vsize),
                "child_feerate": float(feerate_satvbyte),
                "total_fees": total_fees,
                "total_vsizes": total_vsizes,
                "total_feerate": total_feerate,
                "desired_total_feerate": float(fee_rate),
                "sendrawtransaction_command": f"bitcoin-cli sendrawtransaction {final_tx_hex}"
            }
            plugin.log("Dry run: transaction not sent. Type the word `yolo` after the address or use `-k` with `yolo=yolo` to broadcast.")

    return response

plugin.run()
