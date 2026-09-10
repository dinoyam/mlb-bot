import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import requests

# Set DISCORD_WEBHOOK_URL in Render: Dashboard > your service > Environment.
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

if not DISCORD_WEBHOOK_URL:
    raise SystemExit(
        "DISCORD_WEBHOOK_URL is not set. Add it under Environment in Render."
    )

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"
GAME_FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"

sent_alerts = set()
score_snapshots = {}
game_statuses = {}
final_alerts = set()
seeded_games = set()


def log(message):
    # flush=True or Render buffers stdout and the log looks empty.
    print(message, flush=True)


def post_to_discord(content):
    """Post to the webhook, backing off when Discord rate limits us."""
    payload = {"content": content}

    for attempt in range(3):
        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)

            if response.status_code == 429:
                retry_after = 1.0

                try:
                    retry_after = float(response.json().get("retry_after", 1))
                except Exception:
                    pass

                log(f"Rate limited by Discord, waiting {retry_after}s")
                time.sleep(min(retry_after, 30) + 0.25)
                continue

            response.raise_for_status()
            time.sleep(0.5)  # webhooks allow ~30 messages/minute
            return True

        except Exception as post_error:
            log(f"Discord post failed (attempt {attempt + 1}): {post_error}")
            time.sleep(2)

    return False


def is_final_status(status):
    return str(status or "").strip().lower() in {"final", "game over"}


def get_run_label(play):
    result = play.get("result", {})
    rbi = result.get("rbi")

    try:
        rbi = int(rbi)
    except (TypeError, ValueError):
        scoring_runners = 0
        for runner in play.get("runners", []):
            details = runner.get("details") or {}
            movement = runner.get("movement") or {}
            if details.get("isScoringEvent") or movement.get("end") == "score":
                scoring_runners += 1
        rbi = scoring_runners or 1

    return {
        1: "Solo",
        2: "2-Run",
        3: "3-Run",
        4: "Grand Slam",
    }.get(rbi, f"{rbi}-Run")


def get_batter_name(feed, matchup, fallback="Unknown Player"):
    batter = matchup.get("batter", {})
    batter_id = batter.get("id")

    # People live under gameData, not liveData.
    people = feed.get("gameData", {}).get("players", {})
    batter_profile = people.get(f"ID{batter_id}", {})

    return batter.get("fullName") or batter_profile.get("fullName", fallback)


def get_home_run_message(feed, play):
    matchup = play.get("matchup", {})
    batter_name = get_batter_name(feed, matchup)

    result = play.get("result", {})
    run_label = f"{get_run_label(play)} Homer"
    raw_description = result.get("description", "Home run")

    linescore = feed.get("liveData", {}).get("linescore", {})
    linescore_teams = linescore.get("teams", {})
    away_score = linescore_teams.get("away", {}).get("runs", 0)
    home_score = linescore_teams.get("home", {}).get("runs", 0)

    game_teams = feed.get("gameData", {}).get("teams", {})
    away_team = game_teams.get("away", {})
    home_team = game_teams.get("home", {})
    away_ticker = away_team.get("abbreviation", away_team.get("name", "AWAY"))
    home_ticker = home_team.get("abbreviation", home_team.get("name", "HOME"))

    return (
        f"⚾ LIVE HOME RUN! ⚾\n"
        f"**{batter_name}** - {run_label}\n"
        f"{raw_description}\n"
        f"{away_ticker} {away_score} - {home_ticker} {home_score} • "
        f"{get_inning_indicator(linescore)} {get_out_dots(play)}"
    )


def get_scoring_runner_count(play):
    count = 0

    for runner in play.get("runners", []):
        details = runner.get("details") or {}
        movement = runner.get("movement") or {}

        if details.get("isScoringEvent") or movement.get("end") == "score":
            count += 1

    return count


def get_play_rbi_count(play):
    rbi = play.get("result", {}).get("rbi")

    try:
        return int(rbi)
    except (TypeError, ValueError):
        return get_scoring_runner_count(play)


def get_latest_scoring_play(all_plays):
    for play in reversed(all_plays):
        if (
            play.get("result", {}).get("event") == "Home Run"
            or get_play_rbi_count(play) > 0
            or get_scoring_runner_count(play) > 0
        ):
            return play

    return {}


def get_inning_indicator(linescore):
    inning_number = linescore.get("currentInning")
    inning_ordinal = linescore.get("currentInningOrdinal")

    if inning_ordinal:
        ordinal = inning_ordinal
    elif isinstance(inning_number, int):
        suffix = "th"

        if inning_number % 100 not in (11, 12, 13):
            suffix = {
                1: "st",
                2: "nd",
                3: "rd",
            }.get(inning_number % 10, "th")

        ordinal = f"{inning_number}{suffix}"
    else:
        ordinal = str(inning_number or "?")

    inning_state = str(linescore.get("inningState", "")).lower()

    if inning_state == "top":
        arrow = "⬆️"
    elif inning_state == "bottom":
        arrow = "⬇️"
    else:
        arrow = "•"

    return f"{arrow}{ordinal}"


def get_out_dots(play):
    outs = play.get("count", {}).get("outs", 0)

    try:
        outs = max(0, min(int(outs), 2))
    except (TypeError, ValueError):
        outs = 0

    return "●" * outs + "○" * (3 - outs)


def get_score_update_message(feed, all_plays, away_score, home_score, linescore):
    scoring_play = get_latest_scoring_play(all_plays)
    result = scoring_play.get("result", {})
    matchup = scoring_play.get("matchup", {})
    batter_name = get_batter_name(feed, matchup, fallback="Unknown Batter")

    rbi_count = get_play_rbi_count(scoring_play)
    event_type = result.get("event", "Scoring Play")

    if rbi_count > 1:
        rbi_event_type = f"{rbi_count}-RBI {event_type}"
    elif rbi_count == 1:
        rbi_event_type = f"RBI {event_type}"
    else:
        rbi_event_type = event_type

    raw_description = result.get("description", "Scoring play")

    game_teams = feed.get("gameData", {}).get("teams", {})
    away_team = game_teams.get("away", {})
    home_team = game_teams.get("home", {})
    away_ticker = away_team.get("abbreviation", away_team.get("name", "AWAY"))
    home_ticker = home_team.get("abbreviation", home_team.get("name", "HOME"))

    return (
        f"⚾ LIVE SCORE UPDATE ⚾\n"
        f"**{batter_name}** - {rbi_event_type}\n"
        f"{raw_description}\n"
        f"{away_ticker} {away_score} - {home_ticker} {home_score} • "
        f"{get_inning_indicator(linescore)} {get_out_dots(scoring_play)}"
    )


def check_scores():
    """One full pass over today's schedule."""
    date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    schedule_data = requests.get(
        SCHEDULE_URL.format(date=date),
        timeout=10,
    ).json()

    games = [
        game
        for schedule_date in schedule_data.get("dates", [])
        for game in schedule_date.get("games", [])
    ]

    # Process every game independently so one bad game feed cannot stop
    # the remaining games from being checked.
    for game in games:
        status_data = game.get("status", {})
        status = status_data.get("abstractGameState")
        detailed_status = status_data.get("detailedState")
        game_pk = game.get("gamePk")
        previous_status = game_statuses.get(game_pk)

        # Handle a transition to Final or Game Over once per game.
        if is_final_status(status) or is_final_status(detailed_status):
            try:
                game_statuses[game_pk] = status or detailed_status

                if (
                    previous_status is not None
                    and not is_final_status(previous_status)
                    and game_pk not in final_alerts
                ):
                    game_teams = game.get("teams", {})
                    away_team_data = game_teams.get("away", {})
                    home_team_data = game_teams.get("home", {})
                    away_team = away_team_data.get("team", {})
                    home_team = home_team_data.get("team", {})

                    away_ticker = away_team.get(
                        "abbreviation",
                        away_team.get("name", "AWAY"),
                    )
                    home_ticker = home_team.get(
                        "abbreviation",
                        home_team.get("name", "HOME"),
                    )

                    final_message = (
                        f"🏁 FINAL SCORE 🏁\n"
                        f"{away_ticker} {away_team_data.get('score', 0)} @ "
                        f"{home_ticker} {home_team_data.get('score', 0)}\n"
                        f"The game has officially ended."
                    )

                    if post_to_discord(final_message):
                        final_alerts.add(game_pk)

            except Exception as game_error:
                log(f"Error processing final game {game_pk}: {game_error}")

            continue

        game_statuses[game_pk] = status or detailed_status

        # Only process live, ongoing games.
        if status != "Live":
            continue

        try:
            feed = requests.get(
                GAME_FEED_URL.format(game_pk=game_pk),
                timeout=10,
            ).json()

            all_plays = feed.get("liveData", {}).get("plays", {}).get(
                "allPlays",
                [],
            )

            # First time we see this game (including after a restart) we only
            # record what has already happened instead of alerting on it.
            is_first_look = game_pk not in seeded_games

            # Track score changes independently from home-run alerts.
            live_data = feed.get("liveData", {})
            linescore = live_data.get("linescore", {})
            linescore_teams = linescore.get("teams", {})
            away_linescore = linescore_teams.get("away", {})
            home_linescore = linescore_teams.get("home", {})

            away_score = away_linescore.get("runs", 0)
            home_score = home_linescore.get("runs", 0)
            current_scores = (away_score, home_score)
            previous_scores = score_snapshots.get(game_pk)

            latest_scoring_play = get_latest_scoring_play(all_plays)
            latest_event = latest_scoring_play.get("result", {}).get("event")

            # A home run gets only the home-run alert, not a generic
            # score-update alert.
            if (
                previous_scores is not None
                and current_scores != previous_scores
                and latest_event != "Home Run"
            ):
                post_to_discord(
                    get_score_update_message(
                        feed,
                        all_plays,
                        away_score,
                        home_score,
                        linescore,
                    )
                )

            score_snapshots[game_pk] = current_scores

            # Check every play, newest first, for unreported home runs.
            for play in reversed(all_plays):
                result = play.get("result", {})
                about = play.get("about", {})

                if result.get("event") != "Home Run":
                    continue

                play_key = about.get("playId") or about.get("atBatIndex")
                play_id = f"{game_pk}_{play_key}"

                if play_id in sent_alerts:
                    continue

                if is_first_look:
                    # Already happened before we started watching.
                    sent_alerts.add(play_id)
                    continue

                if post_to_discord(get_home_run_message(feed, play)):
                    sent_alerts.add(play_id)

            seeded_games.add(game_pk)

        except Exception as game_error:
            log(f"Error processing game {game_pk}: {game_error}")
            continue


def run_bot():
    log("⚾ Home Run Bot is running live...")

    while True:
        try:
            check_scores()
        except Exception as error:
            log(f"Error checking scores: {error}")

        time.sleep(15)


BODY = b"MLB alert bot is running"


class HealthHandler(BaseHTTPRequestHandler):
    """Render web services require an open port or the deploy is killed."""

    protocol_version = "HTTP/1.1"

    def _send_headers(self):
        # Content-Length is required, or proxies treat the reply as invalid.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()

    def do_GET(self):
        self._send_headers()
        self.wfile.write(BODY)

    def do_HEAD(self):
        self._send_headers()

    def log_message(self, *args):
        pass  # keep Render logs readable


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    log(f"Health server listening on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()
