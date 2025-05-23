# fmt: off
"""
add typevar
"""
from typing import Tuple, TypeVar

# Foo = TypeVar("Foo",  Tuple)  
Const_T = TypeVar("Const_T", int, float, str, bytes, Tuple)
NEW = TypeVar("NEW", int)

def const(expr: Const_T, /) -> Const_T:
    """
    Used to declare that the expression is a constant so that the compiler can
    optimise it.  The use of this function should be as follows::
    """
    ...


def bar(): ...
