"""Explicit local calendar estimates, authored with neutral item/currency IDs.

These policies are not publisher schedules and are never enabled implicitly.
Original board titles, tile artwork and native IDs belong to a separate adapter.
"""
from .catalog import identifier, integer


def split_inventory_awards(item_id, quantity, days=7):
    identifier(item_id)
    integer(days, 1, 4096)
    integer(quantity, days)
    return [{'inventory': {item_id: quantity // days + int(index < quantity % days)}} for index in range(days)]


def estimated_boards(character_fragment, promotion_currency, donation_currency, promotion_level, first_order=0):
    for key in (character_fragment, promotion_currency, donation_currency):
        identifier(key)
    integer(promotion_level, 1, 10000)
    integer(first_order, 0, 999998)
    return {
        'weekly-fragments': {'mode': 'consecutive', 'required_level': 1, 'order': first_order,
            'rewards': [{'inventory': {character_fragment: amount}} for amount in (5, 5, 5, 5, 10, 10, 10)]},
        'daily-promotion': {'mode': 'repeat', 'required_level': promotion_level, 'order': first_order + 1,
            'rewards': [{'wallet': {promotion_currency: 200}} for _ in range(7)]},
        'daily-donations': {'mode': 'repeat', 'required_level': 1, 'order': first_order + 2, 'requires_alliance': True,
            'rewards': [{'wallet': {donation_currency: 10}} for _ in range(7)]}}
