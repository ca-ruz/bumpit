# BumpIt Plugin

A Core Lightning plugin to create CPFP (Child Pays For Parent) transactions for opening lightning channels.

## Prerequisites

- Python 3.7+
- Core Lightning 24.11+
- Bitcoin Core
- txindex in Bitcoin Core

Note: This plugin does not work with lightning version 24.08 because it uses the listaddresses rpc call, which was introduced in version 24.11.

## Installation

1. Clone the repository:
```bash
git clone https://github.com/ca-ruz/bumpit.git
cd bumpit
```

2. Create and activate a virtual environment:
```bash
python -m venv .venv
source .venv/bin/activate
```

3. Install dependencies:
```bash
# Install plugin dependencies
pip install -r requirements.txt

# Install test dependencies
pip install -r requirements-dev.txt
```

## Usage

1. Make sure you are running Bitcoin Core and Core Lightning

2. Start the plugin:
```bash
lightning-cli plugin start $PWD/bumpit.py
```

3. Find a peer ID you want to open a channel with

4. Open a channel:
```bash
lightning-cli fundchannel <peer_id> <amount_in_sats> [feerate]
```

5. Get the funding transaction details:
```bash
lightning-cli listfunds
```

6. Create a CPFP transaction:
```bash
lightning-cli bumpchannelopen <txid> <vout> <fee_rate> [yolo]
```

Note: `fee_rate` should be specified in sat/vB.

Optional: Type the word `yolo` as an argument after the `fee_rate` or use `-k` with `yolo=yolo` if you want the plugin to broadcast the transaction for you.

## Emergency Reserve Warning
The `bumpchannelopen` command may spend outputs that contribute to the node's 25,000 sat emergency reserve. If the fee is too high, this could reduce unreserved funds below the required reserve, potentially affecting node operation. A warning is issued in the response, and the operation is blocked unless `yolo` mode is used. See the `yolo` option in [Plugin Configuration](#plugin-configuration) for details. Always verify unreserved funds with `listfunds` before bumping.

## Running Tests

The test suite uses Core Lightning's test framework and requires a regtest environment.
You need to run the following commands inside of the plugin directory.

1. To run all tests:
```bash
pytest -vs
```

2. To run an individual test:
```bash
pytest -vs <name_of_the_test_file.py>
```

## Manual Testing in Regtest

Note: By default, the `fund_nodes` command in regtest will automatically mine a block, this will confirm the funding transaction. We need to change this in the config, otherwise we wouldn't be able to test the plugin.

### Steps to deactivate minning the block automatically

1. Navigate to Core Lightning's contrib directory:
```bash
cd ~/code/lightning/contrib
```

2. Open the config:
```bash
nano startup_regtest.sh 
```

3. Look for the fund_nodes function and comment out this lines:
```bash
#		"$BCLI" -datadir="$BITCOIN_DIR" -regtest generatetoaddress 6 "$ADDRESS" > /dev/null
#
#		printf "%s" "Waiting for confirmation... "
#
#		while ! "$LCLI" -F --lightning-dir=$LIGHTNING_DIR/l"$node1" listchannels | grep -q "channels"
#		do
#			sleep 1
#		done
```

4. Save & exit

## Steps for testing

1. Start the regtest environment:
```bash
source startup_regtest.sh
start_ln
```

3. Fund the nodes:
```bash
fund_nodes
```

4. Start the plugin (from the plugin directory):
```bash
l1-cli plugin start $PWD/bumpit.py
```

5. Get the funding transaction details:
```bash
l1-cli listfunds
```

6. Create a CPFP transaction:
```bash
l1-cli bumpchannelopen <txid> <vout> <fee_rate> [yolo]
```
Note: `fee_rate` should be specified in sat/vB.
    
Optional: Type the word `yolo` as an argument after the `fee_rate` or use `-k` with `yolo=yolo` if you want the plugin to broadcast the transaction.

## Plugin Configuration

The plugin accepts the following configuration options:

- `bump_brpc_user`: Bitcoin RPC username
- `bump_brpc_pass`: Bitcoin RPC password
- `bump_brpc_port`: Bitcoin RPC port (default: 18443)
- `yolo`: Set to `'yolo'` to bypass the 25,000 sat reserve check. **WARNING**: This may leave your wallet with insufficient funds for other operations. Use with extreme caution.

## Contributing

1. Fork the repository
2. Create a new branch for your feature
3. Make your changes
4. Run the test suite to ensure everything works
5. Submit a pull request
