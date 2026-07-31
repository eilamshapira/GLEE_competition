"""Example agent that lets an LLM choose every move.

This is the pattern most competitive agents start from: hand the model the
game's own prompt plus the machine-readable state, ask for a JSON action, and
never let a bad model response — or a hung provider call — cost you the game:
parse defensively, cap each LLM call with a timeout, and fall back to a safe
valid move.

Works with any provider through litellm (https://docs.litellm.ai): set
LLM_MODEL to any litellm model string and export that provider's API key.

Usage:
    pip install glee-sdk litellm

    export GLEE_API_KEY=glee_...                 # from your dashboard
    export LLM_MODEL=gemini/gemini-2.5-flash     # or gpt-5-mini, claude-sonnet-5, ...
    export GEMINI_API_KEY=...                    # whichever key LLM_MODEL needs

    python llm_agent.py
"""

import json
import logging
import os
import re

from litellm import completion

from glee_sdk import GleeClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("llm_agent")

MODEL = os.environ.get("LLM_MODEL", "gemini/gemini-2.5-flash")

SYSTEM_PROMPT = """\
You are a strategic player in an economic game. You will be shown the game's
rules and situation, the full state visible to you (including the history of
past rounds — use it to model your opponent), and the exact action format.

Play to maximize YOUR OWN payoff over the whole game.

Reply with ONLY a JSON object for your chosen action — no explanation, no
markdown fences, no text before or after the JSON.
"""


def build_user_prompt(game: dict) -> str:
    """Everything the model needs, in one message: the situation as prose,
    the raw state, and the exact shape of a legal reply."""
    return (
        f"{game['prompt']}\n\n"
        f"Full game state visible to you (includes `history` of past rounds):\n"
        f"{json.dumps(game['game_state'], indent=2)}\n\n"
        f"Your action must follow this format:\n"
        f"{json.dumps(game['valid_actions'], indent=2)}\n\n"
        f"Reply with the JSON action only."
    )


def parse_action(text: str) -> dict:
    """Extract the JSON object from a model reply.

    Models sometimes wrap JSON in ```fences``` or add a stray sentence; strip
    fences first, then fall back to the outermost {...} span.
    """
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def safe_action(game: dict) -> dict:
    """A guaranteed-valid conservative move, used when the LLM call or parsing
    fails. Burning one move on this is far cheaper than burning all five
    attempts on malformed JSON, or the 120s turn clock on retries.
    """
    family = game["game_family"]
    action_type = game["valid_actions"]["type"]
    state = game["game_state"]

    if action_type == "offer":
        if family == "bargaining":
            half = state["money_to_divide"] / 2
            return {"alice_gain": half, "bob_gain": half}
        me = state["current_player"]                     # negotiation
        return {"product_price": state[f"{me}_value"]}   # offer your own valuation
    if action_type == "seller_message":
        return {"message": "I recommend this product."}

    # A decision — the valid values differ by family:
    if family == "bargaining":
        return {"decision": "accept"}
    if family == "negotiation":
        return {"decision": "AcceptOffer"}
    return {"decision": "yes"}  # persuasion: recommend / buy


def strategy(game: dict) -> dict:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(game)},
    ]

    # Two tries: if the first reply doesn't parse, show the model its own
    # reply and the error so it can correct itself. After that, play safe.
    for attempt in range(2):
        text = ""
        try:
            # timeout=60 keeps a hanging provider call well inside the 120s
            # turn clock, so the fallback below can still play a safe move.
            response = completion(model=MODEL, messages=messages, timeout=60)
            text = response.choices[0].message.content or ""
            action = parse_action(text)
            logger.info("[%s] %s -> %s", game["game_family"], game["valid_actions"]["type"], action)
            return action
        except Exception as e:  # provider error, timeout, or unparseable reply
            logger.warning("LLM attempt %d failed: %s", attempt + 1, e)
            if attempt == 0:
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": f"Your reply was not a valid JSON action ({e}). "
                               f"Reply with ONLY the corrected JSON object.",
                })

    fallback = safe_action(game)
    logger.warning("Falling back to safe action: %s", fallback)
    return fallback


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
    # An LLM call takes seconds, so play several games in parallel — while one
    # game waits on the model, the others keep moving.
    client.run(strategy, concurrency=8)
