import base64

from algosdk.future import transaction
from algosdk.atomic_transaction_composer import (
    AtomicTransactionComposer,
    AccountTransactionSigner,
    TransactionWithSigner,
)

from beaker.sandbox import get_accounts, get_client
from beaker.client import ApplicationClient

from amm import ConstantProductAMM


client = get_client()

addr, sk = get_accounts().pop()
signer = AccountTransactionSigner(sk)


def demo():

    # Initialize Application from amm.py
    app = ConstantProductAMM()

    # Create an Application client containing both an algod client and my app
    app_client = ApplicationClient(client, app, signer=signer)

    # Create the applicatiion on chain, set the app id for the app client
    app_id, app_addr, txid = app_client.create()
    print(f"Created App with id: {app_id} and address addr: {app_addr} in tx: {txid}")

    # Fund App address so it can create the pool token and hold balances
    sp = client.suggested_params()
    txid = client.send_transaction(
        transaction.PaymentTxn(addr, sp, app_addr, int(1e7)).sign(sk)
    )
    transaction.wait_for_confirmation(client, txid, 4)

    # Create assets
    asset_a = create_asset(addr, sk, "A")
    asset_b = create_asset(addr, sk, "B")
    print(f"Created asset a/b with ids: {asset_a}/{asset_b}")

    # Call app to create pool token
    print("Calling bootstrap")
    result = app_client.call(app.bootstrap, a_asset=asset_a, b_asset=asset_b)
    pool_token = result.return_value
    print(f"Created pool token with id: {pool_token}")
    print_balances(app_id, app_addr, addr, pool_token, asset_a, asset_b)

    # Opt user into token
    sp = client.suggested_params()
    atc = AtomicTransactionComposer()
    atc.add_transaction(
        TransactionWithSigner(
            txn=transaction.AssetTransferTxn(addr, sp, addr, 0, pool_token),
            signer=signer,
        )
    )
    atc.execute(client, 2)
    print_balances(app_id, app_addr, addr, pool_token, asset_a, asset_b)

    ###
    # Fund Pool with initial liquidity
    ###
    print("Funding")
    app_client.call(
        app.mint,
        a_xfer=TransactionWithSigner(
            txn=transaction.AssetTransferTxn(addr, sp, app_addr, 10000, asset_a),
            signer=signer,
        ),
        b_xfer=TransactionWithSigner(
            txn=transaction.AssetTransferTxn(addr, sp, app_addr, 3000, asset_b),
            signer=signer,
        ),
        pool_asset=pool_token,
        a_asset=asset_a,
        b_asset=asset_b,
    )
    print_balances(app_id, app_addr, addr, pool_token, asset_a, asset_b)
    ###
    # Mint pool tokens
    ###
    print("Minting")
    app_client.call(
        app.mint,
        a_xfer=TransactionWithSigner(
            txn=transaction.AssetTransferTxn(addr, sp, app_addr, 100000, asset_a),
            signer=signer,
        ),
        b_xfer=TransactionWithSigner(
            txn=transaction.AssetTransferTxn(addr, sp, app_addr, 1000, asset_b),
            signer=signer,
        ),
        pool_asset=pool_token,
        a_asset=asset_a,
        b_asset=asset_b,
    )
    print_balances(app_id, app_addr, addr, pool_token, asset_a, asset_b)

    ###
    # Swap A for B
    ###
    print("Swapping A for B")
    app_client.call(
        app.swap,
        swap_xfer=TransactionWithSigner(
            txn=transaction.AssetTransferTxn(addr, sp, app_addr, 500, asset_a),
            signer=signer,
        ),
        a_asset=asset_a,
        b_asset=asset_b,
    )
    print_balances(app_id, app_addr, addr, pool_token, asset_a, asset_b)

    ###
    # Swap B for A
    ###
    print("Swapping B for A")
    app_client.call(
        app.swap,
        swap_xfer=TransactionWithSigner(
            txn=transaction.AssetTransferTxn(addr, sp, app_addr, 500, asset_b),
            signer=signer,
        ),
        a_asset=asset_a,
        b_asset=asset_b,
    )
    print_balances(app_id, app_addr, addr, pool_token, asset_a, asset_b)

    ###
    # Burn pool tokens
    ###
    print("Burning")
    app_client.call(
        app.burn,
        pool_xfer=TransactionWithSigner(
            txn=transaction.AssetTransferTxn(addr, sp, app_addr, 100, pool_token),
            signer=signer,
        ),
        pool_asset=pool_token,
        a_asset=asset_a,
        b_asset=asset_b,
    )
    print_balances(app_id, app_addr, addr, pool_token, asset_a, asset_b)


def create_asset(addr, pk, unitname):
    # Get suggested params from network
    sp = client.suggested_params()
    # Create the transaction
    create_txn = transaction.AssetCreateTxn(
        addr, sp, 1000000, 0, False, asset_name="asset", unit_name=unitname
    )
    # Ship it
    txid = client.send_transaction(create_txn.sign(pk))
    # Wait for the result so we can return the app id
    result = transaction.wait_for_confirmation(client, txid, 4)
    return result["asset-index"]


def print_balances(app_id: int, app: str, addr: str, pool: int, a: int, b: int):

    addrbal = client.account_info(addr)
    print("print_balances ==========>")
    print("Participant: ")
    for asset in addrbal["assets"]:
        if asset["asset-id"] == pool:
            print("\tPool Balance {}".format(asset["amount"]))
        if asset["asset-id"] == a:
            print("\tAssetA Balance {}".format(asset["amount"]))
        if asset["asset-id"] == b:
            print("\tAssetB Balance {}".format(asset["amount"]))

    appbal = client.account_info(app)
    print("App: ")
    for asset in appbal["assets"]:
        if asset["asset-id"] == pool:
            print("\tPool Balance {}".format(asset["amount"]))
        if asset["asset-id"] == a:
            print("\tAssetA Balance {}".format(asset["amount"]))
        if asset["asset-id"] == b:
            print("\tAssetB Balance {}".format(asset["amount"]))

    app_state = client.application_info(app_id)
    state = {}
    for sv in app_state["params"]["global-state"]:
        key = base64.b64decode(sv["key"]).decode("utf-8")
        match sv["value"]["type"]:
            case 1:
                val = f"0x{base64.b64decode(sv['value']['bytes']).hex()}"
            case 2:
                val = sv["value"]["uint"]
            case 3:
                pass
        state[key] = val

    if "r" in state:
        print(
            f"\tCurrent ratio a/b == {state['r'] / 1000}"
        )  # TODO: dont hardcode the scale
    else:
        print("\tNo ratio a/b")

    print("print_balances <==========")


if __name__ == "__main__":
    demo()
