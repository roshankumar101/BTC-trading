from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable

from nautilus_trader.model.enums import OptionKind
from nautilus_trader.model.instruments import CryptoOption


def select_btc_atm_pair(
    instruments: Iterable[object],
    reference_price: Decimal,
    now_ns: int,
) -> tuple[CryptoOption, CryptoOption] | None:
    grouped: dict[
        tuple[int, Decimal, str, str],
        dict[OptionKind, CryptoOption],
    ] = defaultdict(dict)

    for instrument in instruments:
        if not isinstance(instrument, CryptoOption):
            continue
        if instrument.underlying.code != "BTC" or instrument.expiration_ns <= now_ns:
            continue
        if instrument.option_kind not in (OptionKind.CALL, OptionKind.PUT):
            continue

        key = (
            instrument.expiration_ns,
            Decimal(str(instrument.strike_price)),
            instrument.quote_currency.code,
            instrument.settlement_currency.code,
        )
        grouped[key][instrument.option_kind] = instrument

    pairs = [
        (key, legs)
        for key, legs in grouped.items()
        if OptionKind.CALL in legs and OptionKind.PUT in legs
    ]
    if not pairs:
        return None

    expiry_ns = min(key[0] for key, _ in pairs)
    nearest_expiry_pairs = [
        (key, legs) for key, legs in pairs if key[0] == expiry_ns
    ]
    _, legs = min(
        nearest_expiry_pairs,
        key=lambda item: (
            abs(item[0][1] - reference_price),
            item[0][1],
            item[0][2],
            item[0][3],
        ),
    )
    return legs[OptionKind.CALL], legs[OptionKind.PUT]
