"""Simple example agent that plays all three GLEE game families.

Usage:
    pip install glee-sdk
    export GLEE_API_KEY=glee_...   # from your dashboard at glee-competition.com
    python simple_agent.py
"""

import logging
import os

from glee_sdk import GleeClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


def strategy(game: dict) -> dict:
    """A simple strategy that handles all three game families."""
    family = game["game_family"]
    actions = game["valid_actions"]
    state = game["game_state"]

    if family == "bargaining":
        return bargaining_strategy(actions, state)
    elif family == "negotiation":
        return negotiation_strategy(actions, state)
    elif family == "persuasion":
        return persuasion_strategy(actions, state)
    else:
        raise ValueError(f"Unknown game family: {family}")


def bargaining_strategy(actions: dict, state: dict) -> dict:
    """50/50 split proposer; accept if getting >= 40%."""
    money = state["money_to_divide"]

    if actions["type"] == "offer":
        half = money / 2
        return {"alice_gain": half, "bob_gain": half, "message": "Fair split?"}

    if actions["type"] == "decision":
        offer = state["last_offer"]
        # In the decision phase, current_player is always the offer's receiver.
        my_gain = offer[f"{state['current_player']}_gain"]

        if my_gain >= money * 0.4:
            return {"decision": "accept"}
        return {"decision": "reject"}

    return {}


def negotiation_strategy(actions: dict, state: dict) -> dict:
    """Offer midpoint; accept if profitable."""
    me = state["current_player"]            # on your turn, that's you
    role = state[f"{me}_role"]              # "seller" or "buyer"
    my_value = state[f"{me}_value"]        # your own valuation — always visible to you

    if actions["type"] == "offer":
        if role == "seller":
            return {"product_price": my_value * 1.5, "message": "Good price"}
        else:
            return {"product_price": my_value * 0.7, "message": "Fair offer"}

    if actions["type"] == "decision":
        offer_price = state["last_offer"]["price"]
        if role == "buyer":
            if offer_price <= my_value:
                return {"decision": "AcceptOffer"}
            return {"decision": "RejectOffer", "product_price": my_value * 0.8}
        else:  # seller
            if offer_price >= my_value:
                return {"decision": "AcceptOffer"}
            return {"decision": "RejectOffer", "product_price": my_value * 1.3}

    return {}


def persuasion_strategy(actions: dict, state: dict) -> dict:
    """Seller: always recommend. Buyer: buy if price < expected value."""
    if actions["type"] == "seller_message":
        return {"message": "This is a great product, I highly recommend it!"}

    if actions["type"] == "seller_recommendation":
        return {"decision": "yes"}

    if actions["type"] == "buyer_decision":
        price = state["product_price"]
        v = state["v"]  # your value for a HIGH-quality unit (GLEE notation; always visible)
        u = state["u"]  # your value for a LOW-quality unit
        p = state["p"]  # the prior P(high quality) — the buyer always knows it
        expected_value = p * v + (1 - p) * u
        return {"decision": "yes" if expected_value > price else "no"}

    return {}


if __name__ == "__main__":
    api_key = os.environ.get("GLEE_API_KEY", "")
    if not api_key:
        print("Set GLEE_API_KEY environment variable")
        exit(1)

    # Defaults to the production server (https://glee-competition.com). Set
    # GLEE_API_URL only when pointing at a local backend during development.
    base_url = os.environ.get("GLEE_API_URL")
    client = (
        GleeClient(api_key=api_key, base_url=base_url)
        if base_url
        else GleeClient(api_key=api_key)
    )

    print(f"Agent stats: {client.stats()}")
    client.run(strategy)
