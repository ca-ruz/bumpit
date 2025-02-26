# BumpChannelOpen Plugin

A Core Lightning plugin to create CPFP (Child Pays For Parent) transactions for opening lightning channels.

## Prerequisites

- Python 3.7+
- Core Lightning node installed and configured
- Bitcoin Core (for regtest environment)

## Installation

1. Clone the repository:

```bash
git clone https://github.com/yourusername/bumpchannelopen.git
cd bumpchannelopen
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

## Running Tests

The test suite uses Core Lightning's test framework and requires a regtest environment.

You need to run the following commands inside of the plugin directory.

To run all tests:

```bash
pytest -vs
```

To run an individual test:

```bash
pytest -vs <name_of_the_test_file.py>
```

## Manual Testing in Regtest

1. Navigate to Core Lightning's contrib directory:
```bash
cd ~/code/lightning/contrib
```

2. Start the regtest environment:
```bash
source startup_regtest.sh
start_ln
```

3. Fund the nodes:
```bash
fund_nodes
```

4. Start the plugin:
```bash
l1-cli plugin start $PWD/bumpchannelopen.py
```

5. Get the funding transaction details:
```bash
l1-cli listfunds
```

6. Create a CPFP transaction:
```bash
l1-cli bumpchannelopen <txid> <vout> <fee_rate> "$(l1-cli newaddr | jq -r '.bech32')"
```
Note: `fee_rate` should be specified in sat/vB

## Plugin Configuration

The plugin accepts the following configuration options:

- `bump_brpc_user`: Bitcoin RPC username
- `bump_brpc_pass`: Bitcoin RPC password
- `bump_brpc_port`: Bitcoin RPC port (default: 18443)

## Contributing

1. Fork the repository
2. Create a new branch for your feature
3. Make your changes
4. Run the test suite to ensure everything works
5. Submit a pull request

## License

[Add your license information here]

## Support

[Add support information or contact details]
