import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# Store your Discord Webhook URL in the DISCORD_WEBHOOK_URL Replit Secret
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"
GAME_FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"

sent_alerts = set()
score_snapshots = {}
game_statuses = {}
final_alerts = set()

print("⚾ Home Run Bot is running live...")


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


def get_home_run_message(feed, play):
    matchup = play.get("matchup", {})
    batter = matchup.get("batter", {})
    batter_id = batter.get("id")

    people = feed.get("liveData", {}).get("people", {})
    batter_profile = people.get(str(batter_id), {})
    batter_name = batter.get("fullName") or batter_profile.get(
        "fullName", "Unknown Player"
    )

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
    batter = matchup.get("batter", {})
    batter_id = batter.get("id")

    people = feed.get("liveData", {}).get("people", {})
    batter_profile = people.get(str(batter_id), {})
    batter_name = batter.get("fullName") or batter_profile.get(
        "fullName", "Unknown Batter"
    )

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


while True:
    try:
        # Grab all games on today's Eastern Time schedule.
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

                        response = requests.post(
                            DISCORD_WEBHOOK_URL,
                            json={"content": final_message},
                            timeout=10,
                        )
                        response.raise_for_status()
                        final_alerts.add(game_pk)

                except Exception as game_error:
                    print(
                        f"Error processing final game {game_pk}: {game_error}"
                    )

                continue

            game_statuses[game_pk] = status or detailed_status

            # Only process live, ongoing games.
            if status == "Live":
                try:
                    feed = requests.get(
                        GAME_FEED_URL.format(game_pk=game_pk),
                        timeout=10,
                    ).json()

                    all_plays = feed.get("liveData", {}).get("plays", {}).get(
                        "allPlays",
                        [],
                    )

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
                    latest_event = latest_scoring_play.get(
                        "result",
                        {},
                    ).get("event")

                    # A home run gets only the home-run alert, not a generic
                    # score-update alert.
                    if (
                        previous_scores is not None
                        and current_scores != previous_scores
                        and latest_event == "Home Run"
                    ):
                        pass
                    elif (
                        previous_scores is not None
                        and current_scores != previous_scores
                    ):
                        score_message = get_score_update_message(
                            feed,
                            all_plays,
                            away_score,
                            home_score,
                            linescore,
                        )

                        response = requests.post(
                            DISCORD_WEBHOOK_URL,
                            json={"content": score_message},
                            timeout=10,
                        )
                        response.raise_for_status()

                    score_snapshots[game_pk] = current_scores

                    # Check every play, newest first, for unreported home runs.
                    for play in reversed(all_plays):
                        result = play.get("result", {})
                        about = play.get("about", {})

                        if result.get("event") == "Home Run":
                            play_id = f"{game_pk}_{about.get('playId')}"

                            if play_id not in sent_alerts:
                                message = get_home_run_message(feed, play)
                                payload = {"content": message}

                                requests.post(
                                    DISCORD_WEBHOOK_URL,
                                    json=payload,
                                    timeout=10,
                                )

                                sent_alerts.add(play_id)

                except Exception as game_error:
                    print(f"Error processing game {game_pk}: {game_error}")
                    continue

    except Exception as error:
        print(f"Error checking scores: {error}")

    time.sleep(15)
