import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import requests

# Set DISCORD_WEBHOOK_URL in Render: Dashboard > your service > Environment.
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

if not DISCORD_WEBHOOK_URL:
    raise SystemExit(
        "DISCORD_WEBHOOK_URL is not set. Add it under Environment in Render."
    )

SCHEDULE_RANGE_URL = (
    "https://statsapi.mlb.com/api/v1/schedule"
    "?sportId=1&startDate={start_date}&endDate={end_date}"
)
SCHEDULE_DAY_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"
GAME_FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"

# Embed sidebar colors.
HOME_RUN_COLOR = 0xE8A33D   # amber
SCORE_COLOR = 0x4B8BF5      # blue
FINAL_COLOR = 0x8A8F98      # gray

sent_alerts = set()
score_snapshots = {}
game_statuses = {}
final_alerts = set()
seeded_games = set()

# Health tracking, surfaced on the status page.
last_cycle_at = None
cycle_count = 0
last_error = None
bot_thread = None

# Discord delivery tracking.
posts_ok = 0
posts_failed = 0
posts_skipped = 0
last_post_at = None
last_post_error = None
blocked_until = 0
live_now = 0
game_lines = []
sched_total = 0
sched_range = ""

# Games MLB's schedule endpoint omits (seen with split doubleheaders).
# Found by probing IDs next to a known split game, and kept for the day.
discovered_pks = set()
probed_pks = set()
probe_day = None

# Manual escape hatch: set EXTRA_GAME_PKS="824785,824786" in Render.
EXTRA_GAME_PKS = {
    int(pk.strip())
    for pk in os.environ.get("EXTRA_GAME_PKS", "").split(",")
    if pk.strip().isdigit()
}

# How long to stay quiet after Discord IP-blocks us.
BLOCK_COOLDOWN = 900

# Days of schedule to request. Covers late games past midnight and
# postponed or resumed games still filed under an earlier date.
LOOKBACK_DAYS = 4


def log(message):
    # flush=True or Render buffers stdout and the log looks empty.
    print(message, flush=True)


def post_to_discord(payload):
    """Post to the webhook, backing off when Discord rate limits us."""
    global posts_ok, posts_failed, posts_skipped
    global last_post_at, last_post_error, blocked_until

    if isinstance(payload, str):
        payload = {"content": payload}

    # Discord has IP-blocked us. Sending more requests only extends the
    # block, so drop the message rather than queue a flood for later.
    if time.time() < blocked_until:
        posts_skipped += 1
        return True

    for attempt in range(5):
        response = None

        try:
            response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)

            if response.status_code == 429:
                retry_after = 1.0
                scope = response.headers.get("X-RateLimit-Scope", "?")
                body = response.text[:160]

                try:
                    retry_after = float(response.json().get("retry_after", 1))
                except Exception:
                    retry_after = float(response.headers.get("Retry-After", 60))

                last_post_error = (
                    f"{datetime.now(ZoneInfo('America/New_York')):%H:%M:%S} - "
                    f"429 scope={scope} retry_after={retry_after:.0f}s body={body}"
                )

                # A global IP block is not our webhook's quota. Retrying
                # makes it last longer, so stop entirely for a while.
                if "blocked from accessing" in body or '"code": 0' in body or '"code":0' in body:
                    blocked_until = time.time() + BLOCK_COOLDOWN
                    posts_failed += 1
                    log(
                        f"Discord IP block. Pausing posts for "
                        f"{BLOCK_COOLDOWN // 60} minutes."
                    )
                    return False

                log(f"Rate limited: {last_post_error}")
                time.sleep(min(retry_after, 120) + 0.5)
                continue

            response.raise_for_status()
            time.sleep(0.5)  # webhooks allow ~30 messages/minute

            posts_ok += 1
            last_post_at = time.time()
            return True

        except Exception as post_error:
            detail = str(post_error)

            # Discord explains rejections in the body; the status code alone
            # does not say which field it disliked.
            if response is not None:
                detail = f"{detail} | {response.text[:200]}"

            log(f"Discord post failed (attempt {attempt + 1}): {detail}")
            last_post_error = (
                f"{datetime.now(ZoneInfo('America/New_York')):%H:%M:%S} - {detail}"
            )
            time.sleep(2)

    posts_failed += 1
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


def build_embed(color, header, title, description, score_line, inning, outs):
    """Shared embed shape so every alert type looks the same."""
    return {
        "embeds": [
            {
                "color": color,
                "author": {"name": header},
                "title": title,
                "description": description,
                "fields": [
                    {"name": "Score", "value": score_line, "inline": True},
                    {"name": "Inning", "value": inning, "inline": True},
                    {"name": "Outs", "value": outs, "inline": True},
                ],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }


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

    return build_embed(
        color=HOME_RUN_COLOR,
        header="⚾ HOME RUN",
        title=f"{batter_name} — {run_label}",
        description=raw_description,
        score_line=f"{away_ticker} {away_score} · {home_ticker} {home_score}",
        inning=get_inning_indicator(linescore),
        outs=get_out_dots(play),
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

    return build_embed(
        color=SCORE_COLOR,
        header="⚾ SCORE UPDATE",
        title=f"{batter_name} — {rbi_event_type}",
        description=raw_description,
        score_line=f"{away_ticker} {away_score} · {home_ticker} {home_score}",
        inning=get_inning_indicator(linescore),
        outs=get_out_dots(scoring_play),
    )


def schedule_entry_from_feed(game_pk):
    """Ask the game feed directly about one ID and shape it like a
    schedule entry, so a game missing from the schedule can still be
    tracked by the normal loop."""
    try:
        feed = requests.get(
            GAME_FEED_URL.format(game_pk=game_pk),
            timeout=10,
        ).json()
    except Exception:
        return None

    game_data = feed.get("gameData", {})
    status = game_data.get("status", {})

    if not status:
        return None

    teams = game_data.get("teams", {})
    linescore_teams = (
        feed.get("liveData", {}).get("linescore", {}).get("teams", {})
    )

    def side(name):
        return {
            "team": {
                "abbreviation": teams.get(name, {}).get("abbreviation"),
                "name": teams.get(name, {}).get("name", "?"),
            },
            "score": linescore_teams.get(name, {}).get("runs", 0),
        }

    return {
        "gamePk": game_pk,
        "gameDate": game_data.get("datetime", {}).get("dateTime", ""),
        "gameNumber": game_data.get("game", {}).get("gameNumber", "?"),
        "doubleHeader": game_data.get("game", {}).get("doubleHeader", "?"),
        "status": {
            "abstractGameState": status.get("abstractGameState"),
            "detailedState": status.get("detailedState"),
        },
        "teams": {"away": side("away"), "home": side("home")},
    }


def find_missing_doubleheader_games(games, seen_pks, today):
    """MLB's schedule has been seen to return only one game of a split
    doubleheader. The twin sits at an adjacent ID, so probe either side
    of any split game and keep whatever turns out to be real."""
    global probe_day

    if probe_day != today:
        # New day, forget yesterday's probing.
        probe_day = today
        probed_pks.clear()
        discovered_pks.clear()

    candidates = set()

    for game in games:
        if str(game.get("doubleHeader", "N")).upper() in {"S", "Y"}:
            pk = game.get("gamePk")

            if isinstance(pk, int):
                candidates.update({pk - 1, pk + 1})

    found = []

    # Manual IDs are adopted unconditionally — no probing rules apply.
    for pk in sorted(EXTRA_GAME_PKS | discovered_pks):
        if pk in seen_pks:
            continue

        entry = schedule_entry_from_feed(pk)

        if not entry:
            continue

        discovered_pks.add(pk)
        seen_pks.add(pk)
        found.append(entry)

    for pk in sorted(candidates):
        if pk in seen_pks or pk in probed_pks:
            continue

        entry = schedule_entry_from_feed(pk)

        if entry is None:
            # Could be a transient failure, so do NOT blacklist it —
            # let the next cycle try again.
            continue

        entry_date = str(entry.get("gameDate", ""))[:10]
        state = entry.get("status", {}).get("abstractGameState")

        # Adopt only a game being played today. A real answer about a
        # game we do not want is a definitive no, so stop probing it.
        if entry_date != today and state != "Live":
            probed_pks.add(pk)
            continue

        discovered_pks.add(pk)
        seen_pks.add(pk)
        found.append(entry)
        log(f"Adopted game {pk} missing from the schedule listing.")

    return found


def check_scores():
    """One full pass over today's schedule."""
    global live_now, game_lines, sched_total, sched_range

    live_count = 0
    lines = []

    now = datetime.now(ZoneInfo("America/New_York"))

    # Two requests, merged. The range form covers late games past midnight
    # and postponed games filed under an earlier date, but it has been seen
    # to omit games — notably game one of a split doubleheader. The
    # single-date form returns those, so ask for both and take the union.
    start_date = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    payloads = []

    for url in (
        SCHEDULE_RANGE_URL.format(start_date=start_date, end_date=end_date),
        SCHEDULE_DAY_URL.format(date=end_date),
        SCHEDULE_DAY_URL.format(date=start_date),
    ):
        try:
            payloads.append(requests.get(url, timeout=10).json())
        except Exception as sched_error:
            log(f"Schedule fetch failed for {url}: {sched_error}")

    # The same game can only appear once, but dedupe by gamePk to be safe.
    games = []
    seen_pks = set()

    for schedule_data in payloads:
        for schedule_date in schedule_data.get("dates", []):
            for game in schedule_date.get("games", []):
                game_pk = game.get("gamePk")

                if game_pk in seen_pks:
                    continue

                seen_pks.add(game_pk)
                games.append(game)

    games.extend(find_missing_doubleheader_games(games, seen_pks, end_date))

    sched_total = len(games)
    sched_range = f"{start_date} to {end_date} ({len(payloads)} queries)"

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

                    away_runs = away_team_data.get("score", 0) or 0
                    home_runs = home_team_data.get("score", 0) or 0

                    away_line = f"{away_ticker} {away_runs}"
                    home_line = f"{home_ticker} {home_runs}"

                    # Bold whichever side won. A tie leaves both plain.
                    try:
                        if int(away_runs) > int(home_runs):
                            away_line = f"**{away_line}**"
                        elif int(home_runs) > int(away_runs):
                            home_line = f"**{home_line}**"
                    except (TypeError, ValueError):
                        pass

                    final_message = {
                        "embeds": [
                            {
                                "color": FINAL_COLOR,
                                "author": {"name": "🏁 FINAL"},
                                "title": f"{away_line}  @  {home_line}",
                                "timestamp": datetime.now(
                                    timezone.utc
                                ).isoformat(),
                            }
                        ]
                    }

                    if post_to_discord(final_message):
                        final_alerts.add(game_pk)

            except Exception as game_error:
                log(f"Error processing final game {game_pk}: {game_error}")

            continue

        game_statuses[game_pk] = status or detailed_status

        # Only process live, ongoing games.
        # Record what MLB says about every game we are shown, so a game
        # that gets filtered out is visible rather than silently absent.
        try:
            teams_block = game.get("teams", {})
            away_nm = teams_block.get("away", {}).get("team", {}).get(
                "abbreviation"
            ) or teams_block.get("away", {}).get("team", {}).get("name", "?")
            home_nm = teams_block.get("home", {}).get("team", {}).get(
                "abbreviation"
            ) or teams_block.get("home", {}).get("team", {}).get("name", "?")

            dh_flag = game.get("doubleHeader", "?")
            game_no = game.get("gameNumber", "?")
            game_date = str(game.get("gameDate", ""))[:10]

            if not (is_final_status(status) or is_final_status(detailed_status)):
                lines.append(
                    f"{away_nm}@{home_nm} [{status}/{detailed_status}] "
                    f"pk={game_pk} g{game_no} dh={dh_flag} {game_date}"
                )
        except Exception:
            pass

        # MLB files Warmup and Pre-Game under the "Live" umbrella. No plays
        # exist yet, so skip them or the live count reads high.
        if status != "Live" or detailed_status in {"Warmup", "Pre-Game"}:
            continue

        live_count += 1

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

    live_now = live_count
    game_lines = lines


def run_bot():
    global last_cycle_at, cycle_count, last_error

    log("⚾ Home Run Bot is running live...")

    while True:
        try:
            check_scores()
            last_cycle_at = time.time()
            cycle_count += 1

            # A heartbeat every ~5 minutes so the Render log shows life
            # without drowning in noise.
            if cycle_count % 20 == 0:
                log(f"Heartbeat: {cycle_count} cycles, watching {len(seeded_games)} games")

        except Exception as error:
            last_error = f"{datetime.now(ZoneInfo('America/New_York')):%H:%M:%S} - {error}"
            log(f"Error checking scores: {error}")

        time.sleep(15)


def watchdog():
    """Restart the bot loop if its thread ever dies."""
    global bot_thread

    while True:
        time.sleep(30)

        if bot_thread is None or not bot_thread.is_alive():
            log("Bot thread died. Restarting it.")
            bot_thread = threading.Thread(target=run_bot, daemon=True)
            bot_thread.start()
            continue

        # Thread alive but not completing cycles means it is wedged,
        # usually on a request that never returns.
        if last_cycle_at and time.time() - last_cycle_at > 300:
            log("Bot loop has not completed a cycle in 5 minutes.")


STATUS_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>MLB Alert Bot</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; min-height: 100vh; display: flex;
    align-items: center; justify-content: center;
    background: #16181c; color: #e6e8eb;
    font: 15px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
    padding: 24px;
  }}
  .card {{
    width: 100%; max-width: 420px;
    background: #1e2126; border: 1px solid #2c3038;
    border-radius: 12px; padding: 24px 26px;
  }}
  h1 {{
    margin: 0 0 20px; font-size: 15px; font-weight: 600;
    letter-spacing: .04em; text-transform: uppercase; color: #f0f2f4;
    display: flex; align-items: center; gap: 10px;
  }}
  .dot {{
    width: 9px; height: 9px; border-radius: 50%;
    background: {dot}; box-shadow: 0 0 8px {dot};
  }}
  .row {{
    display: flex; justify-content: space-between;
    gap: 16px; padding: 9px 0; border-top: 1px solid #262a31;
  }}
  .row:first-of-type {{ border-top: 0; }}
  .k {{ color: #878e99; }}
  .v {{ color: #f0f2f4; text-align: right; word-break: break-word; }}
  .err {{ color: #e5807a; }}
  footer {{ margin-top: 18px; font-size: 12px; color: #6b727d; }}
  .games {{
    margin-top: 14px; padding-top: 12px; border-top: 1px solid #262a31;
    font-size: 12px; color: #9aa2ad; line-height: 1.7;
  }}
</style></head>
<body><div class="card">
  <h1><span class="dot"></span>MLB Alert Bot</h1>
  <div class="row"><span class="k">bot loop</span><span class="v">{alive}</span></div>
  <div class="row"><span class="k">last check</span><span class="v">{last_seen}</span></div>
  <div class="row"><span class="k">live games now</span><span class="v">{live}</span></div>
  <div class="row"><span class="k">total checks</span><span class="v">{cycles}</span></div>
  <div class="row"><span class="k">games seen</span><span class="v">{games}</span></div>
  <div class="row"><span class="k">posts sent ok</span><span class="v">{posts_ok}</span></div>
  <div class="row"><span class="k">posts failed</span><span class="v {fail_class}">{posts_failed}</span></div>
  <div class="row"><span class="k">dropped while blocked</span><span class="v">{posts_skipped}</span></div>
  <div class="row"><span class="k">discord block</span><span class="v {block_class}">{block_state}</span></div>
  <div class="row"><span class="k">last post</span><span class="v">{last_post}</span></div>
  <div class="row"><span class="k">last post error</span><span class="v {post_err_class}">{post_error}</span></div>
  <div class="row"><span class="k">last loop error</span><span class="v {err_class}">{error}</span></div>
  <div class="games">{games_block}</div>
  <footer>refreshes every 15s</footer>
</div></body></html>
"""


def humanize_age(stamp):
    if not stamp:
        return "never"

    seconds = int(time.time() - stamp)

    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"

    return f"{seconds // 86400}d ago"


def build_status():
    if last_cycle_at:
        seconds_ago = int(time.time() - last_cycle_at)
        last_seen = f"{seconds_ago}s ago"
        healthy = seconds_ago < 60
    else:
        last_seen = "never"
        healthy = False

    alive = bot_thread is not None and bot_thread.is_alive()
    healthy = healthy and alive

    remaining = int(blocked_until - time.time())

    if remaining > 0:
        block_state = f"clears in {remaining // 60}m {remaining % 60}s"
    else:
        block_state = "clear"

    if game_lines:
        header = f"schedule: {sched_total} games, {sched_range}<br><br>"
        games_block = header + "<br>".join(
            line.replace("&", "&amp;").replace("<", "&lt;") for line in game_lines
        )
    else:
        games_block = f"schedule: {sched_total} games, {sched_range}<br><br>no games in progress"

    html = STATUS_TEMPLATE.format(
        dot="#57c07d" if healthy else "#e5807a",
        alive="running" if alive else "STOPPED",
        last_seen=last_seen,
        live=live_now,
        cycles=cycle_count,
        games=len(seeded_games),
        posts_ok=posts_ok,
        posts_failed=posts_failed,
        fail_class="err" if posts_failed else "",
        posts_skipped=posts_skipped,
        block_state=block_state,
        block_class="err" if remaining > 0 else "",
        last_post=humanize_age(last_post_at),
        post_error=last_post_error or "none",
        post_err_class="err" if last_post_error else "",
        error=last_error or "none",
        err_class="err" if last_error else "",
        games_block=games_block,
    )

    return html.encode("utf-8")


class HealthHandler(BaseHTTPRequestHandler):
    """Render web services require an open port or the deploy is killed."""

    protocol_version = "HTTP/1.1"

    def _send_headers(self, length):
        # Content-Length is required, or proxies treat the reply as invalid.
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_GET(self):
        body = build_status()
        self._send_headers(len(body))
        self.wfile.write(body)

    def do_HEAD(self):
        self._send_headers(len(build_status()))

    def log_message(self, *args):
        pass  # keep Render logs readable


if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    threading.Thread(target=watchdog, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    log(f"Health server listening on port {port}")
    ThreadingHTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()
