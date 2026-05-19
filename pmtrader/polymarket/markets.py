"""Polymarket market constants.

Each Market pairs the token IDs with the tick size so callers can't mix tick
and token incorrectly (a real footgun — pandemic uses 0.001 tick, vaccine 0.01).

The `pmt` CLI's --token resolver looks these up by name:
    pmt buy --token hantavirus-pandemic:no --price 0.93 --size 217
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    name: str
    condition_id: str
    yes_token: str
    no_token: str
    tick_size: str  # str because py_clob_client_v2 wants it that way


HANTAVIRUS_PANDEMIC = Market(
    name="Hantavirus pandemic in 2026?",
    condition_id="0xa4ddc18895cc7b14810283ef8f113939abffd3969c6a0e37f1897110c67e6f73",
    yes_token="51508280778202349361616850684455231843716212176724253736363122559269229712002",
    no_token="95212449865986159112377413335252801281670333750637442556685159781445406848396",
    tick_size="0.001",
)

HANTAVIRUS_VACCINE = Market(
    name="Hantavirus vaccine in 2026?",
    condition_id="0x5b334c510f1b9f4d7cf7ebf1eeeff9cb7b16106027d921bd4e2bf742110be02b",
    yes_token="33574848766046164159312361389126746625941229104553637902902710371273925289603",
    no_token="65212199459705189540054970689860634578415205165846935633340159883171881149961",
    tick_size="0.01",
)

CORONAVIRUS_PANDEMIC = Market(
    name="New Coronavirus Pandemic in 2026?",
    condition_id="0x3dec83132c57c32848641f9fdf6e87687f7a5f7f4bdd8f33368bb5fdecb96b74",
    yes_token="27515921066882013223295256333819020556672789094219444894322459359838815569517",
    no_token="90702790936057338053502316835437449327217840723095157272143342998269881818958",
    tick_size="0.001",
)

HANTAVIRUS_LAB_LEAK = Market(
    name="Hantavirus lab leak confirmed by June 30?",
    condition_id="0x4cd71e7b8c5c3f7cd1fc025002b2982aae1e5f70a65bf1faaae121299127537b",
    yes_token="15464130699531603675372044350692986464780699477441200262642102173111827234544",
    no_token="77911208241982327373495855644935587349201177208106713081551029073015187679590",
    tick_size="0.001",
)
