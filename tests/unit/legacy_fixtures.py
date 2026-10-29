"""Legacy (pre-VM) contract code fixtures shared by the legacy migration tests."""

# Shaped exactly like the legacy compiler's output (see legacy
# contracting.compilation.compiler.ContractingCompiler).
LEGACY_TOKEN = """\
__balances = Hash(default_value=0, contract='con_tok', name='balances')
__owner = Variable(contract='con_tok', name='owner')
__TransferEvent = LogEvent(event='Transfer', params={'to': {'type': str, 'idx': True}}, contract='con_tok', name='TransferEvent')
RATE = decimal('0.5')


def ____(vk: str):
    __owner.set(vk)
    __balances[vk] = 100


def __require_owner():
    assert ctx.caller == __owner.get(), 'Not owner'


@__export('con_tok')
def transfer(amount: float, to: str):
    assert __balances[ctx.caller] >= amount
    __balances[ctx.caller] -= amount
    __balances[to] += amount
    __TransferEvent({'to': to})


@__export('con_tok')
def mint(amount: float):
    __require_owner()
    __balances[ctx.caller] += amount * RATE


@__export('con_tok')
def peek(token: str):
    __other = ForeignHash(foreign_contract=token, foreign_name='balances', contract='con_tok', name='other')
    return __other[ctx.caller]
"""
