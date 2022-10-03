from typing import Final
from pyteal import (
    abi,
    TealType,
    Bytes,
    Global,
    Expr,
    Int,
    Seq,
    Assert,
    Txn,
    And,
    ScratchVar,
    AssetHolding,
    AssetParam,
    WideRatio,
    If,
    Or,
    InnerTxn,
    InnerTxnBuilder,
    TxnField,
    Concat,
    TxnType,
    Sqrt,
)

from beaker import (
    consts,
    ApplicationStateValue,
    Application,
    Authorize,
    external,
    create,
    internal,
)

# WARNING: THIS IS NOT PROODUCTION LEVEL CODE
# Seriously, there are _definitely_ bugs in the math


class ConstantProductAMM(Application):

    # Declare Application state, marking `Final` here so the python class var doesn't get changed
    # Marking a var `Final` does _not_ change anything at the AVM level
    governor: Final[ApplicationStateValue] = ApplicationStateValue(
        stack_type=TealType.bytes,
        key=Bytes("g"),
        default=Global.creator_address(),
        descr="The current governor of this contract, allowed to do admin type actions",
    )
    asset_a: Final[ApplicationStateValue] = ApplicationStateValue(
        stack_type=TealType.uint64,
        key=Bytes("a"),
        static=True,
        descr="The asset id of asset A",
    )
    asset_b: Final[ApplicationStateValue] = ApplicationStateValue(
        stack_type=TealType.uint64,
        key=Bytes("b"),
        static=True,
        descr="The asset id of asset B",
    )
    pool_token: Final[ApplicationStateValue] = ApplicationStateValue(
        stack_type=TealType.uint64,
        key=Bytes("p"),
        static=True,
        descr="The asset id of the Pool Token, used to track share of pool the holder may recover",
    )
    ratio: Final[ApplicationStateValue] = ApplicationStateValue(
        stack_type=TealType.uint64,
        key=Bytes("r"),
        descr="The ratio between assets (A/B)*Scale",
    )

    ##############
    # Constants
    ##############

    # Total supply of the pool tokens
    _total_supply: Final[int] = int(1e10)
    total_supply: Final[Expr] = Int(_total_supply)
    # scale helps with precision when doing computation for
    # the number of tokens to transfer
    _scale: Final[int] = 1000
    scale: Final[Expr] = Int(_scale)
    # Fee for swaps, 5 represents 0.5% ((fee / scale)*100)
    _fee: Final[int] = 5
    fee: Final[Expr] = Int(_fee)

    ##############
    # Administrative Actions
    ##############

    # Call this only on create
    @create
    def create(self):
        return self.initialize_application_state()

    # Only the account set in app_state.governor may call this method
    @external(authorize=Authorize.only(governor))
    def set_governor(self, new_governor: abi.Account):
        """sets the governor of the contract, may only be called by the current governor"""
        return self.governor.set(new_governor.address())

    # Only the account set in app_state.governor may call this method
    @external(authorize=Authorize.only(governor))
    def bootstrap(
        self,
        seed: abi.PaymentTransaction,
        a_asset: abi.Asset,
        b_asset: abi.Asset,
        *,
        output: abi.Uint64,
    ):
        """bootstraps the contract by opting into the assets and creating the pool token"""

        # Requirements:  Is it correct to allow repeated `bootstrap` invocations that duplicate pool creation + opt-in?
        return Seq(
            Assert(
                Global.group_size() == Int(2),
                seed.get().receiver() == self.address,
                seed.get().amount() >= consts.Algos(0.3),
                a_asset.asset_id() < b_asset.asset_id(), # Request for clarification:  What motivates requiring ordering of asset IDs?
            ),
            self.asset_a.set(a_asset.asset_id()),
            self.asset_b.set(b_asset.asset_id()),
            self.pool_token.set(
                self.do_create_pool_token(
                    self.asset_a,
                    self.asset_b,
                ),
            ),
            self.do_opt_in(self.asset_a),
            self.do_opt_in(self.asset_b),
            output.set(self.pool_token),
        )

    ##############
    # AMM specific methods for mint/burn/swap
    ##############

    @external
    def mint(
        self,
        a_xfer: abi.AssetTransferTransaction,
        b_xfer: abi.AssetTransferTransaction,
        pool_asset: abi.Asset = pool_token,
        a_asset: abi.Asset = asset_a,
        b_asset: abi.Asset = asset_b,
    ):
        """mint pool tokens given some amount of asset A and asset B"""

        well_formed_mint = [
            a_asset.asset_id() == self.asset_a,
            b_asset.asset_id() == self.asset_b,
            pool_asset.asset_id() == self.pool_token,
        ]

        valid_asset_a_xfer = [
            a_xfer.get().asset_receiver() == self.address,
            a_xfer.get().xfer_asset() == self.asset_a,
            a_xfer.get().asset_amount() > Int(0),
            a_xfer.get().sender() == Txn.sender(),
        ]

        valid_asset_b_xfer = [
            b_xfer.get().asset_receiver() == self.address,
            b_xfer.get().xfer_asset() == self.asset_b,
            b_xfer.get().asset_amount() > Int(0),
            b_xfer.get().sender() == Txn.sender(),
        ]

        return Seq(
            # Check that the transaction is constructed correctly
            Assert(*well_formed_mint),
            Assert(*valid_asset_a_xfer),
            Assert(*valid_asset_b_xfer),
            # Check that we have these things
            pool_bal := pool_asset.holding(self.address).balance(),
            a_bal := a_asset.holding(self.address).balance(),
            b_bal := b_asset.holding(self.address).balance(),
            Assert(And(pool_bal.hasValue(), a_bal.hasValue(), b_bal.hasValue())),
            # mint tokens
            self.do_axfer(
                Txn.sender(),
                self.pool_token,
                If(
                    And(
                        a_bal.value() == a_xfer.get().asset_amount(),
                        b_bal.value() == b_xfer.get().asset_amount(),
                    ),
                    # This is the first time we've been called
                    # we use a different formula to mint tokens
                    self.tokens_to_mint_initial(
                        a_xfer.get().asset_amount(), b_xfer.get().asset_amount()
                    ),
                    # Normal mint
                    self.tokens_to_mint(
                        self.total_supply - pool_bal.value(), # Is it safe to omit underflow check?
                        a_bal.value() - a_xfer.get().asset_amount(),
                        b_bal.value() - b_xfer.get().asset_amount(),
                        a_xfer.get().asset_amount(),
                        b_xfer.get().asset_amount(),
                    ),
                ),
            ),
            self.ratio.set(self.get_ratio()),
        )

    @external
    def burn(
        self,
        pool_xfer: abi.AssetTransferTransaction,
        pool_asset: abi.Asset = pool_token,
        a_asset: abi.Asset = asset_a,
        b_asset: abi.Asset = asset_b,
    ):
        """burn pool tokens to get back some amount of asset A and asset B"""

        well_formed_burn = [
            pool_asset.asset_id() == self.pool_token,
            a_asset.asset_id() == self.asset_a,
            b_asset.asset_id() == self.asset_b,
        ]

        valid_pool_xfer = [
            pool_xfer.get().asset_receiver() == self.address,
            pool_xfer.get().asset_amount() > Int(0),
            pool_xfer.get().xfer_asset() == self.pool_token,
            pool_xfer.get().sender() == Txn.sender(),
        ]

        return Seq(
            Assert(*well_formed_burn),
            Assert(*valid_pool_xfer),
            pool_bal := pool_asset.holding(self.address).balance(),
            a_bal := a_asset.holding(self.address).balance(),
            b_bal := b_asset.holding(self.address).balance(),
            Assert(And(pool_bal.hasValue(), a_bal.hasValue(), b_bal.hasValue())),
            # Get the total number of tokens issued (prior to receiving the current axfer of pool tokens)
            (issued := ScratchVar()).store(
                self.total_supply - (pool_bal.value() - pool_xfer.get().asset_amount()) # Correctness:  Check for underflow.
            ),
            # Send back commensurate amt of a
            self.do_axfer(
                Txn.sender(),
                self.asset_a,
                self.tokens_to_burn(
                    issued.load(),
                    a_bal.value(),
                    pool_xfer.get().asset_amount(),
                ),
            ),
            # Send back commensurate amt of b
            self.do_axfer(
                Txn.sender(),
                self.asset_b,
                self.tokens_to_burn(
                    issued.load(),
                    b_bal.value(),
                    pool_xfer.get().asset_amount(),
                ),
            ),
            self.ratio.set(self.get_ratio())
            # Should the ratio should be the same before and after?
            # Assert(self.ratio == self.get_ratio()),
        )

    @external
    def swap(
        self,
        swap_xfer: abi.AssetTransferTransaction,
        a_asset: abi.Asset = asset_a,
        b_asset: abi.Asset = asset_b,
    ):
        """Swap some amount of either asset A or asset B for the other"""
        well_formed_swap = [
            a_asset.asset_id() == self.asset_a,
            b_asset.asset_id() == self.asset_b,
        ]

        valid_swap_xfer = [
            Or(
                swap_xfer.get().xfer_asset() == self.asset_a,
                swap_xfer.get().xfer_asset() == self.asset_b,
            ),
            swap_xfer.get().asset_amount() > Int(0),
            swap_xfer.get().sender() == Txn.sender(),
        ]

        out_id = If(
            swap_xfer.get().xfer_asset() == self.asset_a,
            self.asset_b,
            self.asset_a,
        )
        in_id = swap_xfer.get().xfer_asset()

        return Seq(
            Assert(*well_formed_swap),
            Assert(*valid_swap_xfer),
            in_sup := AssetHolding.balance(self.address, in_id),
            out_sup := AssetHolding.balance(self.address, out_id),
            Assert(And(in_sup.hasValue(), out_sup.hasValue())),
            self.do_axfer(
                Txn.sender(),
                out_id,
                self.tokens_to_swap(
                    swap_xfer.get().asset_amount(),
                    in_sup.value() - swap_xfer.get().asset_amount(),
                    out_sup.value(),
                ),
            ),
            self.ratio.set(self.get_ratio()),
        )

    ##############
    # Mathy methods
    ##############

    @internal(TealType.uint64)
    def tokens_to_mint(self, issued, a_supply, b_supply, a_amount, b_amount):
        return Seq(
            (a_rat := ScratchVar()).store(
                WideRatio([a_amount, self.scale], [a_supply])
            ),
            (b_rat := ScratchVar()).store(
                WideRatio([b_amount, self.scale], [b_supply])
            ),
            WideRatio(
                [If(a_rat.load() < b_rat.load(), a_rat.load(), b_rat.load()), issued],
                [self.scale],
            ),
        )

    @internal(TealType.uint64)
    def tokens_to_mint_initial(self, a_amount, b_amount):
        return Sqrt(a_amount * b_amount) - self.scale

    @internal(TealType.uint64)
    def tokens_to_burn(self, issued, supply, amount):
        return WideRatio([supply, amount], [issued])

    @internal(TealType.uint64)
    def tokens_to_swap(self, in_amount, in_supply, out_supply):
        factor = self.scale - self.fee
        return WideRatio(
            [in_amount, factor, out_supply],
            [(in_supply * self.scale) + (in_amount * factor)],
        )

    ##############
    # Utility methods for inner transactions
    ##############

    @internal(TealType.none)
    def do_axfer(self, rx, aid, amt):
        return InnerTxnBuilder.Execute(
            {
                TxnField.type_enum: TxnType.AssetTransfer,
                TxnField.xfer_asset: aid,
                TxnField.asset_amount: amt,
                TxnField.asset_receiver: rx,
            }
        )

    @internal(TealType.none)
    def do_opt_in(self, aid):
        return self.do_axfer(self.address, aid, Int(0))

    @internal(TealType.uint64)
    def do_create_pool_token(self, a, b):
        return Seq(
            una := AssetParam.unitName(a),
            unb := AssetParam.unitName(b),
            Assert(And(una.hasValue(), unb.hasValue())),
            InnerTxnBuilder.Execute(
                {
                    TxnField.type_enum: TxnType.AssetConfig,
                    TxnField.config_asset_name: Concat(
                        Bytes("DPT-"), una.value(), Bytes("-"), unb.value()
                    ),
                    TxnField.config_asset_unit_name: Bytes("dpt"),
                    TxnField.config_asset_total: self.total_supply,
                    TxnField.config_asset_decimals: Int(3),
                    TxnField.config_asset_manager: self.address,
                    TxnField.config_asset_reserve: self.address,
                }
            ),
            InnerTxn.created_asset_id(),
        )

    @internal(TealType.uint64)
    def get_ratio(self):
        return Seq(
            bal_a := AssetHolding.balance(
                self.address,
                self.asset_a,
            ),
            bal_b := AssetHolding.balance(
                self.address,
                self.asset_b,
            ),
            Assert(And(bal_a.hasValue(), bal_b.hasValue())),
            WideRatio([bal_a.value(), self.scale], [bal_b.value()]),
        )


if __name__ == "__main__":
    ConstantProductAMM().dump("artifacts")
