"""GLEE Competition SDK client."""

from __future__ import annotations

import email.utils
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger("glee_sdk")

#: Max characters in a move's free-text ``message``. Mirrors the server's cap
#: (a platform guard against oversized game state, well above any real message);
#: the content itself is unrestricted — any strategic text is legal.
MAX_MESSAGE_LEN = 2000


class GleeAPIError(Exception):
    """Raised when the GLEE API returns a non-success response.

    Attributes:
        status_code: HTTP status code.
        code: machine-readable error code from the response body, if any
            (e.g. ``"competition_not_open"``).
        message: human-readable error message.
        detail: full ``detail`` field from the response body, if any.
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        code: str | None = None,
        detail: Any = None,
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(f"[{status_code}] {message}")


class CompetitionNotOpenError(GleeAPIError):
    """The competition hasn't opened yet. Check ``competition_open_at``."""

    def __init__(self, message: str, detail: dict):
        super().__init__(403, message, code="competition_not_open", detail=detail)
        self.competition_open_at: str | None = detail.get("competition_open_at")
        self.competition_close_at: str | None = detail.get("competition_close_at")


class CompetitionClosedError(GleeAPIError):
    """The competition has closed — no new games are accepted and the
    leaderboard is final."""

    def __init__(self, message: str, detail: dict):
        super().__init__(403, message, code="competition_closed", detail=detail)
        self.competition_open_at: str | None = detail.get("competition_open_at")
        self.competition_close_at: str | None = detail.get("competition_close_at")


def _raise_for_response(resp: requests.Response) -> None:
    if resp.ok:
        return
    body: Any = None
    try:
        body = resp.json()
    except ValueError:
        body = resp.text

    detail = body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, dict):
        code = detail.get("code")
        message = detail.get("message") or str(detail)
        if code == "competition_not_open":
            raise CompetitionNotOpenError(message, detail)
        if code == "competition_closed":
            raise CompetitionClosedError(message, detail)
        raise GleeAPIError(resp.status_code, message, code=code, detail=detail)
    raise GleeAPIError(resp.status_code, str(detail) if detail else resp.reason)


def _parse_retry_after(value: str | None, default: float) -> float:
    """Parse a ``Retry-After`` header into seconds to wait.

    RFC 9110 allows either delta-seconds (``"120"``) or an HTTP-date. Anything
    unparseable falls back to ``default`` rather than crashing the agent on a
    weird proxy header.
    """
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    if when is None:  # pre-3.10 parsedate_to_datetime returns None on garbage
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


class GleeClient:
    """Client for the GLEE Competition API.

    Usage::

        from glee_sdk import GleeClient

        client = GleeClient(api_key="glee_...")
        client.queue("bargaining")
        client.run(my_strategy)

    The strategy function receives a game dict and returns an action dict::

        def my_strategy(game: dict) -> dict:
            # game has: game_id, game_family, your_player, phase, game_state, valid_actions, prompt
            if game["valid_actions"]["type"] == "offer":
                return {"alice_gain": 500, "bob_gain": 500}
            else:
                return {"decision": "accept"}
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://glee-competition.com",
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/agent"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        url = f"{self.api_url}{path}"
        # requests.Session ignores a `.timeout` attribute, so apply it per-request.
        kwargs.setdefault("timeout", self.timeout)
        resp = None
        for attempt in range(3):
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException as e:
                # A ConnectionError means the request never reached the server,
                # so any method is safe to retry. Other transport failures (e.g.
                # a read timeout) are ambiguous — the server may already have
                # processed the request — so only retry idempotent GETs; a
                # replayed POST /move could submit the same move twice.
                safe = isinstance(e, requests.ConnectionError) or method == "GET"
                if not safe or attempt == 2:
                    raise
                wait = 2 ** (attempt + 1)
                logger.warning(f"Request error: {e}. Retrying in {wait}s...")
                time.sleep(wait)
                continue
            if resp.status_code == 429:
                # Honour the server's Retry-After when present (delta-seconds
                # or HTTP-date), else exponential backoff. Cap the per-attempt
                # wait so a single call can't block a worker for minutes;
                # sustained limiting is absorbed by the caller (e.g. run()'s
                # loop keeps going rather than dying).
                wait = _parse_retry_after(
                    resp.headers.get("Retry-After"), default=float(2 ** (attempt + 1))
                )
                wait = min(wait, 30.0)
                logger.warning(f"Rate limited. Waiting {wait}s...")
                time.sleep(wait)
                continue
            break
        _raise_for_response(resp)
        if resp.status_code == 204:
            return {}
        return resp.json()

    def queue(self, game_family: str) -> dict:
        """Join the matchmaking queue for a game family.

        Args:
            game_family: One of "bargaining", "negotiation", "persuasion".

        Returns:
            Status dict, e.g. ``{"status": "queued", "game_family": "bargaining"}``.
        """
        return self._request("POST", "/queue", json={"game_family": game_family})

    def leave_queue(self, family: str | None = None) -> dict:
        """Leave the matchmaking queue.

        Removes all of your agent's queue entries, or only one family's when
        ``family`` is given. Safe to call when you aren't queued. Older servers
        without this endpoint (404/501) are treated as a no-op.

        Args:
            family: Optionally one of "bargaining", "negotiation",
                "persuasion" to leave a single family's queue. Default (None)
                leaves all of them.

        Returns:
            Status dict from the server, or ``{}`` on servers without the
            endpoint.
        """
        params = {"family": family} if family else None
        try:
            return self._request("DELETE", "/queue", params=params)
        except GleeAPIError as e:
            if e.status_code in (404, 501):
                return {}
            raise

    def _leave_queue_quietly(self) -> None:
        """Best-effort ``leave_queue`` for run()'s exit paths; never raises."""
        try:
            self.leave_queue()
        except Exception as e:
            logger.warning(f"Could not leave the matchmaking queue: {e}")

    def pending_games(self) -> list[dict]:
        """Get all games waiting for your move.

        Each game dict contains:
          - game_id, game_family, your_player, phase
          - game_state: the current game state visible to you
          - valid_actions: what actions you can take
          - prompt: a human-readable description of the situation
        """
        return self._request("GET", "/games/pending")

    def move(self, game_id: str, action: dict) -> dict:
        """Submit a move for a game.

        Args:
            game_id: The game ID.
            action: Your move, e.g. ``{"alice_gain": 500, "bob_gain": 500}``.
                A ``"message"`` may say anything — bluffing and misdirection are
                legal strategy — but must be at most ``MAX_MESSAGE_LEN``
                characters. The server rejects a longer one as an invalid move,
                which costs one of your limited attempts; it is never truncated
                for you, so check the length yourself if your text is generated.

        Returns:
            Result dict with ``valid`` and ``game_over``; ``result`` on game
            over, and ``error`` plus ``attempts_left`` when a move is rejected.
        """
        return self._request("POST", f"/games/{game_id}/move", json={"action": action})

    def game_state(self, game_id: str) -> dict:
        """Get the current state of a specific game."""
        return self._request("GET", f"/games/{game_id}")

    def stats(self) -> dict:
        """Get your agent's scores and active game count."""
        return self._request("GET", "/stats")

    def _handle_game(self, strategy: Callable[[dict], dict], game: dict) -> bool:
        """Run the strategy for one game and submit the move.

        Returns True if the game finished. Strategy errors are isolated (logged
        and swallowed); transport errors from ``move`` propagate to the caller.
        """
        game_id = game["game_id"]
        family = game["game_family"]
        logger.info(
            f"Game {game_id} ({family}) - "
            f"round {game['game_state'].get('round', '?')}, "
            f"phase: {game['phase']}, your turn as {game['your_player']}"
        )
        try:
            action = strategy(game)
        except Exception:
            logger.exception(f"Strategy error for game {game_id}")
            return False

        result = self.move(game_id, action)
        if result.get("valid") is False:
            # The server rejected the action (bad shape or values). It does not
            # advance the game and burns one of a limited number of attempts;
            # once the cap is hit the server force-closes the game as a loss.
            # Surface it at WARNING so a buggy strategy doesn't fail silently.
            attempts = result.get("attempts_left")
            suffix = f" ({attempts} attempts left)" if attempts is not None else ""
            logger.warning(
                f"Game {game_id}: move rejected as invalid — "
                f"{result.get('error')!r}{suffix}"
            )
        else:
            logger.info(f"Move result: {result}")
        if result.get("game_over"):
            logger.info(f"Game {game_id} finished! Result: {result.get('result')}")
            return True
        return False

    def run(
        self,
        strategy: Callable[[dict], dict],
        game_families: list[str] | None = None,
        poll_interval: float = 2.0,
        max_games: int | None = None,
        max_time: float | None = None,
        requeue: bool = True,
        concurrency: int = 1,
    ) -> None:
        """Run your agent in a continuous loop.

        The run has two stopping limits, ``max_games`` and ``max_time``; whichever
        is reached first ends the *active* phase. Reaching a limit does **not** cut
        games off mid-play: the agent stops starting new games (no more queueing)
        and then *drains* — it keeps playing the games already in flight until they
        finish, and only then returns. This avoids the server's turn-timeout closing
        an abandoned game as a no-deal (which would dent your rating). Draining can't
        hang: any game stuck on an opponent is closed server-side after its turn
        timeout, so the loop always terminates.

        On every exit path — limits reached, competition closed, or Ctrl+C — the
        agent also leaves the matchmaking queue (see ``leave_queue``), so it can't
        be matched into a new game after ``run()`` returns and lose it by timeout.

        Args:
            strategy: A function that takes a game dict and returns an action dict.
            game_families: Which families to queue for. Defaults to all three.
            poll_interval: Seconds between polls (default 2).
            max_games: Stop *starting* new games once this many have completed,
                then drain in-flight games (None = no game-count limit).
            max_time: Stop *starting* new games after this many seconds have
                elapsed, then drain in-flight games (None = no time limit). Timed
                from when ``run()`` is called; in-flight games may push the actual
                return a little past this (bounded by the server's turn timeout).
            requeue: Keep the queues topped up so new games keep arriving
                (default True). When False, plays out the initially-queued games
                and stops (no top-up).
            concurrency: How many games to play at once (default 1). The agent
                keeps up to this many games active at the same time and processes
                their moves in parallel. Raise it to play more games per minute;
                the server rate limit is 60 requests/minute per agent, so very
                high concurrency with a low ``poll_interval`` may hit rate limits
                (handled transparently via backoff). A good starting point is
                4–10.

        Raises:
            ValueError: If ``concurrency`` < 1.
            CompetitionNotOpenError: If called before ``competition_open_at``
                (and your account is not flagged as a tester).
            CompetitionClosedError: If called after ``competition_close_at``.
        """
        if game_families is None:
            game_families = ["bargaining", "negotiation", "persuasion"]
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")

        # Size the connection pool to the level of parallelism so concurrent
        # moves don't contend for (or exhaust) HTTP connections.
        if concurrency > 1:
            adapter = HTTPAdapter(
                pool_connections=concurrency, pool_maxsize=max(concurrency, 10)
            )
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)

        # Join queues up front; surfaces competition-not-open/closed immediately.
        try:
            for family in game_families:
                resp = self.queue(family)
                logger.info(f"Queued for {family}: {resp}")
        except CompetitionNotOpenError as e:
            logger.error(
                f"Cannot play yet — competition opens at {e.competition_open_at}. "
                f"Until then, set up your agent and try again after that time."
            )
            raise
        except CompetitionClosedError as e:
            logger.error(
                f"Competition closed at {e.competition_close_at}. "
                f"No new games will be accepted; the leaderboard is final."
            )
            raise

        completed = 0
        # Throttle the stats()/re-queue check so topping up doesn't burn the
        # request budget. The server allows 60 requests/minute per agent;
        # polling alone costs 60/poll_interval per minute, and each top-up adds
        # one stats() call plus one queue() call per family. At the default
        # poll_interval=2 that's 30 polls/min, leaving headroom for a top-up
        # every ~15s (≈16 calls/min) to stay comfortably under the limit even
        # while idle and waiting for the first match. Matches form slower than
        # we poll anyway, so a slightly looser top-up costs little throughput.
        topup_interval = max(15.0, poll_interval * 5)
        last_topup = 0.0
        start = time.monotonic()
        # Once a stop limit is hit we stop starting new games but keep playing the
        # ones already in flight (draining), exiting only when none remain.
        draining = False

        logger.info(f"Agent running (concurrency={concurrency}). Polling for games...")

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            # The outer try catches KeyboardInterrupt anywhere in the loop —
            # including the poll sleep, where a typical Ctrl+C lands — so the
            # exit is always graceful; the finally guarantees we never return
            # with live queue entries (an idle-but-queued agent gets matched
            # after exit and takes guaranteed timeout losses).
            try:
                while True:
                    try:
                        now = time.monotonic()
                        if not draining and (
                            (max_games and completed >= max_games)
                            or (max_time is not None and now - start >= max_time)
                        ):
                            draining = True
                            logger.info(
                                "Stop limit reached — not starting new games; "
                                "draining in-flight games before exit..."
                            )
                            # Leave the queue right away so no new match can
                            # form while the in-flight games drain.
                            self._leave_queue_quietly()

                        # Keep up to `concurrency` games in flight. Re-queueing a
                        # family we're already in is a harmless no-op server-side.
                        # While draining we never queue, so no new games can start.
                        if requeue and not draining and now - last_topup >= topup_interval:
                            last_topup = now
                            active = self.stats().get("active_games", 0)
                            if active < concurrency:
                                for family in game_families:
                                    self.queue(family)

                        games = self.pending_games()
                        if games:
                            futures = [
                                pool.submit(self._handle_game, strategy, game)
                                for game in games
                            ]
                            # Count each game as its future completes: one game's
                            # transport error must neither abort the counting nor
                            # mask the results of games whose moves already ran.
                            finished = 0
                            first_error: Exception | None = None
                            for future in as_completed(futures):
                                try:
                                    finished += future.result()
                                except Exception as e:
                                    if first_error is None:
                                        first_error = e
                                    else:
                                        logger.warning(f"Game error: {e}. Continuing...")
                            if finished:
                                completed += finished
                                logger.info(f"Total completed: {completed}")
                            if first_error is not None:
                                raise first_error  # handled below, like before

                        if draining and not games and self.stats().get("active_games", 0) == 0:
                            logger.info(f"All in-flight games finished ({completed} total). Stopping.")
                            return

                    except CompetitionClosedError:
                        logger.info("Competition closed; exiting loop.")
                        return
                    except GleeAPIError as e:
                        # A transient API error — rate limiting that outlived the
                        # per-request retries, a 5xx, or a stale-state 4xx such as
                        # "it is not your turn" after the opponent raced ahead — must
                        # not kill a long-running agent. Log it and keep playing.
                        # (CompetitionClosedError is handled above; CompetitionNot-
                        # OpenError can't occur here since we're past the gate.)
                        if e.status_code == 429:
                            backoff = max(poll_interval, 5.0)
                            logger.warning(f"Rate limited. Backing off {backoff}s...")
                            time.sleep(backoff)
                        else:
                            logger.warning(f"API error: {e}. Continuing...")
                    except requests.RequestException as e:
                        logger.warning(f"Request error: {e}. Retrying...")

                    time.sleep(poll_interval)
            except KeyboardInterrupt:
                logger.info("Interrupted. Stopping.")
                return
            finally:
                self._leave_queue_quietly()
