#!/usr/bin/env python3
from decimal import Decimal
from pyln.client import Plugin, RpcError
import json
from bitcointx.core.psbt import PartiallySignedTransaction
from bitcoinrpc.authproxy import AuthServiceProxy, JSONRPCException
import os
import sys

# import debugpy
# debugpy.listen(("localhost", 5678))

plugin = Plugin()

class CPFPError(Exception):
    """Custom exception for CPFP-related errors"""
    pass

# Plugin configuration options
plugin.add_option('bump_brpc_user', "__cookie__", 'bitcoin rpc user')
plugin.add_option('bump_brpc_pass', None, 'bitcoin rpc password')
plugin.add_option('bump_brpc_port', 18443, 'bitcoin rpc port')
plugin.add_option(
    "yolo",
    None,
    "Set to 'yolo' to broadcast transaction automatically after finalizing the psbt"
)

def connect_bitcoincli():
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
    rpc_user=plugin.get_option('bump_brpc_user')
    rpc_password=plugin.get_option('bump_brpc_pass')
    port=plugin.get_option('bump_brpc_port')
    host="127.0.0.1"
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
        return AuthServiceProxy(rpc_url, timeout=600)
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

def try_unreserve_inputs(plugin, psbt):
    try:
        plugin.rpc.unreserveinputs(psbt=psbt)
        plugin.log("[CLEANUP] Successfully unreserved inputs via PSBT")
    except Exception as e:
        plugin.log(f"[ERROR] UNABLE TO UNRESERVE INPUTS: {e}")

def input_validation(txid, vout, amount, yolo):
    if not isinstance(txid, str) or not txid:
        raise Exception("Invalid or missing txid: must be a non-empty string")
    if not isinstance(vout, int) or vout < 0:
        raise Exception("Invalid vout: must be a non-negative integer")
    if not isinstance(amount, str) or not amount:
        raise Exception("Invalid or missing amount: must be a non-empty string with 'sats' or 'satvb' suffix")
    if not (amount.endswith('sats') or amount.endswith('satvb')):
        raise Exception("Invalid amount: must end with 'sats' or 'satvb'")
    if yolo is not None and yolo != "yolo":
        raise Exception(f"You missed YOLO mode! You passed {yolo} as an argument, but not `yolo`.")

def parse_input(txid, vout, amount):
    fee, fee_rate = 0, Decimal(0)
    try:
        if amount.endswith('sats'):
            fee = int(amount[:-4])  # Remove 'sats' suffix
            if fee < 0:
                raise Exception("Invalid fee: must be non-negative")
            plugin.log(f"[BRAVO-FEE] Using fixed child fee: {fee} sats")
        else:  # amount.endswith('satvb')
            fee_rate = float(amount[:-5])  # Remove 'satvb' suffix
            if fee_rate < 0:
                raise Exception("Invalid fee_rate: must be non-negative")
            plugin.log(f"[BRAVO-FEERATE] Using feerate: {fee_rate} sat/vB")
    except (TypeError, ValueError):
        raise Exception("Invalid amount: must be a valid number followed by 'sats' or 'satvb'")
    plugin.log(f"[DEBUG] Current amount: {amount}, fee_rate: {fee_rate}, fee: {fee}", level="debug")
    plugin.log(f"[BRAVO] Input Parameters - txid: {txid}, vout: {vout}, fee_rate: {fee_rate}")
    return fee, fee_rate

def log_yolo(yolo):    
    if yolo == "yolo":
        plugin.log("YOLO mode is ON!")
    else:
        plugin.log("Safety mode is ON!")
    
def get_new_address():
    address = plugin.rpc.newaddr().get('bech32')
    plugin.log(f"[BRAVO2.0] Got new bech32 address from node: address: {address}")
    return address

def validate_network(): 
    try:
        network = plugin.rpc.getinfo().get('network')
        plugin.log(f"[CHARLIE] Network detected: {network}")
        if not network:
            raise Exception("Network information is missing")
    except RpcError as e:
        plugin.log(f"[SIERRA] RPC Error: {str(e)}")
        raise Exception(f"Failed to fetch network info: {str(e)}")

def get_utxos():
    try:
        funds = plugin.rpc.listfunds()
        plugin.log(f"[INFO] Funds retrieved: {funds}")
        utxos = funds.get("outputs", [])
        if not utxos:
            raise Exception("No unspent transaction outputs found")
    except RpcError as e:
        plugin.log(f"[SIERRA] RPC Error: {str(e)}")
        raise Exception(f"Failed to fetch funds: {str(e)}")
    plugin.log("[DEBUG] All UTXOs before filtering:")
    for idx, utxo in enumerate(utxos):
        reserved_status = utxo.get("reserved", False)
        plugin.log(f"[DEBUG] UTXO {idx}: txid={utxo['txid']} vout={utxo['output']} amount={utxo['amount_msat']} msat, reserved={reserved_status}")
    available_utxos = [utxo for utxo in utxos if not utxo.get("reserved", False)]
    plugin.log("[INFO] Available UTXOs after filtering:")
    if not available_utxos:
        plugin.log("[ECHO] No unreserved UTXOs available")
        raise Exception("No unreserved unspent transaction outputs found")
    for idx, utxo in enumerate(available_utxos):
        plugin.log(f"[FOXTROT] {idx}: txid={utxo['txid']} vout={utxo['output']} amount={utxo['amount_msat']} msat")
    plugin.log(f"[DEBUG] Count of available UTXOs: {len(available_utxos)}")
    if available_utxos:
        plugin.log(f"[DEBUG] Available UTXOs contents: {available_utxos}")
    return funds, available_utxos

def select_utxo(available_utxos, txid, vout):    
    selected_utxo = None
    for utxo in available_utxos:
        if utxo["txid"] == txid and utxo["output"] == vout:
            selected_utxo = utxo
            break
    if not selected_utxo:
        raise Exception(f"UTXO {txid}:{vout} not found in available UTXOs")
    plugin.log(f"[DEBUG] Selected UTXO: txid={selected_utxo['txid']}, vout={selected_utxo['output']}, amount={selected_utxo['amount_msat']} msat")
    return selected_utxo

def parent_tx_details(txid): 
    try:
        rpc_connection = connect_bitcoincli()
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
        raise Exception (f"Failed to fetch transaction: {str(e)}")
    return rpc_connection, tx, parent_fee, parent_fee_rate, parent_vsize

def is_tx_confirmed(tx):
    if tx.get("confirmations", 0) > 0:
        raise Exception ("Transaction is already confirmed and cannot be bumped")

def fetch_utxo_details(selected_utxo, txid, vout):
    amount_msat = selected_utxo["amount_msat"]
    if not amount_msat:
        raise Exception(f"UTXO {txid}:{vout} not found or already spent")
    utxo_amount_btc = amount_msat / 100_000_000_000
    plugin.log(f"[DEBUG] Amount in BTC: {utxo_amount_btc}")
    return utxo_amount_btc

def verify_address(address):    
    try:
        listaddresses_result = plugin.rpc.listaddresses()
        valid_addresses = [
            entry[key] for entry in listaddresses_result.get("addresses", [])
            for key in ("bech32", "p2tr") if key in entry
        ]
        if address not in valid_addresses:
            plugin.log(f"[ERROR] Address {address} is not owned by this node", level="error")
            raise Exception(f"Recipient address {address} is not owned by this node")
    except RpcError as e:
        plugin.log(f"[SIERRA] RPC Error: {str(e)}")
        raise Exception(f"Failed to verify address: {str(e)}")
    plugin.log(f"[INFO] Address {address} is valid and owned by this node")

def create_mock_psbt(selected_utxo, rpc_connection, address, utxo_amount_btc):
    utxo_selector = [{"txid": selected_utxo["txid"], "vout": selected_utxo["output"]}]
    plugin.log(f"[MIKE] Bumping selected output using UTXO {utxo_selector}")
    try:
        rpc_result = rpc_connection.createpsbt(utxo_selector, [{address: utxo_amount_btc}])
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
        raise Exception(f"Failed to create PSBT: {str(e)}")
    except Exception as e:
        plugin.log(f"[ROMEO] Error during PSBT creation: {str(e)}")
        raise Exception(f"Unexpected error during PSBT creation: {str(e)}")
    return first_child_vsize, utxo_selector

def get_childfee_input(amount, available_utxos, fee, fee_rate, parent_fee_rate, parent_fee, parent_vsize, first_child_vsize):
    plugin.log(f"[DEBUG] Before Step 9 - amount: {amount}, type: {type(amount)}", level="debug")
    total_unreserved_sats = sum(utxo["amount_msat"] // 1000 for utxo in available_utxos)
    if amount.endswith('sats'):
        desired_child_fee = fee
        plugin.log(f"[FEE] Using user-specified desired child fee: {desired_child_fee} sats")
    else:  # amount.endswith('satvb')
        target_feerate = fee_rate  # Validation already done
        if parent_fee_rate < target_feerate:
            try:
                desired_child_fee = calculate_child_fee(
                    parent_fee=parent_fee,
                    parent_vsize=parent_vsize,
                    child_vsize=first_child_vsize,
                    desired_total_feerate=target_feerate
                )
                plugin.log(f"[FEE] Calculated desired child fee from feerate: {desired_child_fee} sats")
            except CPFPError as e:
                plugin.log(f"[ROMEO] CPFPError occurred: {str(e)}")
                raise Exception(f"Failed to calculate child fee: {str(e)}")
        else:
            desired_child_fee = 0
            plugin.log(f"[FEE] No CPFP needed based on feerate")
    child_fee = desired_child_fee
    plugin.log(f"[DEBUG] Total unreserved balance: {total_unreserved_sats} sats, estimated child fee: {child_fee} sats")
    return desired_child_fee, total_unreserved_sats, child_fee
   
def validate_emergency_reserve(total_unreserved_sats, child_fee):
    if total_unreserved_sats - child_fee < 25000:
        plugin.log(f"[WARNING] Bump would leave {total_unreserved_sats - child_fee} sats, below 25000 sat emergency reserve.")
        return {
            "code": -32600,
            "message": f"Bump would leave {total_unreserved_sats - child_fee} sats, below 25000 sat emergency reserve.",
            "child_fee": child_fee
        }

def check_feerate(amount, parent_fee_rate, fee_rate, parent_fee, parent_vsize):
    if amount.endswith('satvb') and parent_fee_rate >= fee_rate:
        plugin.log(f"[INFO] Skipping PSBT: parent fee rate {parent_fee_rate:.2f} sat/vB "
                   f"meets or exceeds target {fee_rate:.2f} sat/vB")
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
            "desired_total_feerate": fee_rate
        }

def calc_confirmed_unreserved(funds, vout, desired_child_fee, txid, utxo_amount_btc):
    total_sats = calculate_confirmed_unreserved_amount(funds, txid, vout)
    plugin.log(f"[GOLF] Total amount in confirmed and unreserved outputs: {total_sats} sats")
    utxo_amount_btc = format(utxo_amount_btc, '.8f')
    recipient_amount = float(utxo_amount_btc) - (float(desired_child_fee) / 10**8)
    recipient_amount = format(recipient_amount, '.8f')
    plugin.log(f"[UNIFORM] _utxo_amount_btc: {utxo_amount_btc}, Recipient amount: {recipient_amount}, first_child_fee: {desired_child_fee}")
    return recipient_amount

def create_PSBT(rpc_connection, utxo_selector, address, recipient_amount):
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
        raise Exception(f"Failed to create second PSBT: {str(e)}")
    except Exception as e:
        plugin.log(f"[ROMEO] Error during PSBT creation: {str(e)}")
        raise Exception(f"Unexpected error during second PSBT creation: {str(e)}")
    return second_psbt, second_child_vsize

def reserve_sign_PSBT(second_psbt, rpc_connection):
    try:
        plugin.rpc.reserveinputs(psbt=second_psbt)
        reserved_psbt = second_psbt
        second_signed_psbt = plugin.rpc.signpsbt(psbt=second_psbt)
        plugin.log(f"[DEBUG] signpsbt response: {second_signed_psbt}")
        second_child_psbt = second_signed_psbt.get("signed_psbt", second_signed_psbt.get("psbt"))
        if not second_child_psbt:
            try_unreserve_inputs(plugin, reserved_psbt)
            raise Exception("Signing failed. No signed PSBT returned.")
        plugin.log(f"[DEBUG] Signed PSBT: {second_child_psbt}")
        finalized_psbt = rpc_connection.finalizepsbt(second_child_psbt, False)
        plugin.log(f"[DEBUG] finalized_psbt: {finalized_psbt}")
        finalized_psbt_base64 = finalized_psbt.get("psbt")
        if not finalized_psbt_base64:
            try_unreserve_inputs(plugin, reserved_psbt)
            raise Exception("PSBT was not properly finalized. No PSBT hex returned.")
    except (JSONRPCException, RpcError) as e:
        plugin.log(f"[SIERRA] RPC Error during PSBT signing: {str(e)}")
        try_unreserve_inputs(plugin, reserved_psbt)
        raise Exception(f"Failed to reserve or sign PSBT: {str(e)}")
    except Exception as e:
        plugin.log(f"[ROMEO] Error during PSBT signing: {str(e)}")
        try_unreserve_inputs(plugin, reserved_psbt)
        raise Exception(f"Unexpected error during PSBT signing: {str(e)}")
    return finalized_psbt_base64, reserved_psbt

def analyze_final_tx(rpc_connection, finalized_psbt_base64, second_child_vsize, reserved_psbt):
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
            try_unreserve_inputs(plugin, reserved_psbt)
            raise Exception("Could not extract hex from finalized PSBT.")
        decoded_tx = rpc_connection.decoderawtransaction(final_tx_hex)
        actual_vsize = decoded_tx.get("vsize")
        plugin.log(f"[DEBUG] Actual vsize: {actual_vsize}")
        txid = decoded_tx.get("txid")
        plugin.log(f"[DEBUG] Final transaction ID (txid): {txid}")
    except (JSONRPCException, RpcError) as e:
        plugin.log(f"[SIERRA] RPC Error during transaction analysis: {str(e)}")
        try_unreserve_inputs(plugin, reserved_psbt)
        raise Exception(f"Failed to analyze transaction: {str(e)}")
    except Exception as e:
        plugin.log(f"[ROMEO] Error during transaction analysis: {str(e)}")
        try_unreserve_inputs(plugin, reserved_psbt)
        raise Exception(f"Unexpected error during transaction analysis: {str(e)}")
    return signed_child_fee, feerate_satvbyte, final_tx_hex

def caculate_totals(signed_child_fee, parent_fee, parent_vsize, second_child_vsize):
    child_fee_satoshis = float(signed_child_fee) * 100000000
    total_fees = int(parent_fee) + int(child_fee_satoshis)
    total_vsizes = int(parent_vsize) + int(second_child_vsize)
    total_feerate = total_fees / total_vsizes
    return child_fee_satoshis, total_fees, total_vsizes, total_feerate

def build_response(finalized_psbt_base64, parent_fee, parent_vsize, parent_fee_rate, child_fee_satoshis, second_child_vsize, feerate_satvbyte, total_fees, total_vsizes, total_feerate, fee_rate, amount, final_tx_hex):
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
        "desired_total_feerate": fee_rate if amount.endswith('satvb') else 0,
        "message2": "Run sendrawtransaction to broadcast your cpfp transaction",
        "sendrawtransaction_command": f"bitcoin-cli sendrawtransaction {final_tx_hex}",
        "notice": "Inputs used in this PSBT are now reserved. If you do not broadcast this transaction, you must manually unreserve them",
        "unreserve_inputs_command": f"lightning-cli unreserveinputs {finalized_psbt_base64}",
        #"message3": "Alternatively, you can restart Core Lightning to release all input reservations"
    }
    return response

def yolo_mode(rpc_connection, final_tx_hex, response, reserved_psbt):
    try:
        plugin.log(f"[YOLO] Sending raw transaction...")
        sent_txid = rpc_connection.sendrawtransaction(final_tx_hex)
        plugin.log(f"[YOLO] Transaction sent! TXID: {sent_txid}")
        response["message"] = "You used YOLO mode! Transaction sent! Please run the analyze command to confirm transaction details."
        for key in ["message2", "sendrawtransaction_command", "notice", "unreserve_inputs_command"]:
            del response[key]
        return response
    except (JSONRPCException, RpcError) as e:
        plugin.log(f"[SIERRA] RPC Error during transaction broadcast: {str(e)}")
        try_unreserve_inputs(plugin, reserved_psbt)
        raise Exception(f"Failed to broadcast transaction: {str(e)}")
    except Exception as e:
        plugin.log(f"[ERROR] Error during transaction broadcast: {str(e)}")
        try_unreserve_inputs(plugin, reserved_psbt)
        raise Exception(f"Unexpected error during transaction broadcast: {str(e)}")

@plugin.method("bumpchannelopen",
               desc="Creates a CPFP transaction to bump the feerate of a parent output, with checks for emergency reserve.",
               long_desc="Creates a Child-Pays-For-Parent (CPFP) transaction to increase the feerate of a specified output. "
                         "Use `listfunds` to check unreserved funds before bumping. Amount must end with 'sats' (fixed fee) or 'satvb' (fee rate in sat/vB). "
                         "Use `yolo` mode to broadcast transaction automatically")
@wrap_method
def bumpchannelopen(plugin, txid, vout, amount, yolo=None):
    """
    Creates a CPFP transaction for a specific parent output.

    Args:
        txid: Parent transaction ID (string)
        vout: Output index (non-negative integer)
        amount: Fee amount with suffix (e.g., '1000sats' for fixed fee, '10satvb' for fee rate in sat/vB)
        yolo: Set to 'yolo' to send transaction automatically
    """

    # Validate & Parse input
    input_validation(txid, vout, amount, yolo)
    fee, fee_rate = parse_input(txid, vout, amount)
    log_yolo(yolo)

    # Step 1: Get new address
    address = get_new_address()

    # Step 2: Fetch network information
    validate_network()

    # Step 3: Get list of UTXOs
    funds, available_utxos = get_utxos()

    # Select UTXO
    selected_utxo = select_utxo(available_utxos, txid, vout)

    # Step 4: Calculate parent transaction details
    rpc_connection, tx, parent_fee, parent_fee_rate, parent_vsize = parent_tx_details(txid)

    # Step 5: Check if transaction is confirmed
    is_tx_confirmed(tx)

    # Step 6: Fetch UTXO details
    utxo_amount_btc = fetch_utxo_details(selected_utxo,txid, vout)

    # Step 7: Verify address
    verify_address(address)

    # Step 8: Create first PSBT
    first_child_vsize, utxo_selector = create_mock_psbt(selected_utxo, rpc_connection, address, utxo_amount_btc)

    # Step 9: Calculate child fee and check emergency reserve
    desired_child_fee, total_unreserved_sats, child_fee = get_childfee_input(amount, available_utxos, fee, fee_rate, parent_fee_rate, parent_fee, parent_vsize, first_child_vsize)
    validate_emergency_reserve(total_unreserved_sats, child_fee)

    # Step 10: Check feerate    
    check_feerate(amount, parent_fee_rate, fee_rate, parent_fee, parent_vsize)

    # Step 11: Calculate confirmed unreserved amount
    recipient_amount = calc_confirmed_unreserved(funds, vout, desired_child_fee, txid, utxo_amount_btc)

    # Step 13: Check minimum relay fee
# def check_min_relayfee():
    # MIN_RELAY_FEE = 1.0
    # child_feerate = desired_child_fee / first_child_vsize
    # if child_feerate < MIN_RELAY_FEE:
    #     return {
    #         "code": -32600,
    #         "message": f"Child transaction feerate ({child_feerate:.2f} sat/vB) below minimum relay fee ({MIN_RELAY_FEE} sat/vB). Increase fee_rate."
    #     }
# check_min_relayfee()

    # Step 14: Create second PSBT
    second_psbt, second_child_vsize = create_PSBT(rpc_connection, utxo_selector, address, recipient_amount)

    # Step 15: Reserve and sign PSBT
    finalized_psbt_base64, reserved_psbt = reserve_sign_PSBT(second_psbt, rpc_connection)

    # Step 16: Analyze final transaction
    signed_child_fee, feerate_satvbyte, final_tx_hex = analyze_final_tx(rpc_connection, finalized_psbt_base64, second_child_vsize, reserved_psbt)

    # Step 17: Calculate totals
    child_fee_satoshis, total_fees, total_vsizes, total_feerate = caculate_totals(signed_child_fee, parent_fee, parent_vsize, second_child_vsize)

    # Step 18: Build response
    response = build_response(finalized_psbt_base64, parent_fee, parent_vsize, parent_fee_rate, child_fee_satoshis, second_child_vsize, feerate_satvbyte, total_fees, total_vsizes, total_feerate, fee_rate, amount, final_tx_hex)

    # Step 19: Handle yolo mode
    if yolo is not None and yolo == "yolo":
        response = yolo_mode(rpc_connection, final_tx_hex, response, reserved_psbt)

    return response

plugin.run()
