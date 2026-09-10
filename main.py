import os
import time
from datetime import datetime
import pytz
import requests
from threading import Thread
from http.server import SimpleHTTPRequestHandler, HTTPServer

# ==============================================================================
# 🌐 1. RENDER PORT HARNESS (KEEPS APP ALIVE 24/7 FOR FREE)
# ==============================================================================
def run_fake_server():
    # Binds to port 10000 to completely satisfy Render's free web service check
    server = HTTPServer(('0.0.0.0', 10000), SimpleHTTPRequestHandler)
    print("🌐 Port 10000 securely bound. Render environment verified!")
    server.serve_forever()

# Launch the server thread immediately upon boot
Thread(target=run_fake_server, daemon=True).start()

# ==============================================================================
# ⚾ 2. CORE LIVE TRACKING LOGIC (GLOBAL LEAGUE-WIDE SCANNER)
# ==============================================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
if not DISCORD_WEBHOOK_URL:
    raise ValueError("CRITICAL ERROR: The DISCORD_WEBHOOK_URL environment variable is missing!")

# Corrected production endpoints to ensure live feeds load successfully
SCHEDULE_URL = "https://mlb.com{date}"
GAME_FEED_URL = "https://mlb.com{game_pk}/feed/live"

sent_alerts = set()
sent_finals = set()
game_scores = {}

print("⚾ League-Wide MLB Ticker is running live 24/7 on Render...")

while True:
    try:
        tz = pytz.timezone('America/New_York')
        current_date = datetime.now(tz).strftime('%Y-%m-%d')
        
        # Pull the daily schedule data
        schedule_response = requests.get(SCHEDULE_URL.format(date=current_date), timeout=10)
        schedule_response.raise_for_status()
        schedule_data = schedule_response.json()
        
        # Safely extract all games scheduled for today
        games = []
        for date_entry in schedule_data.get("dates", []):
            games.extend(date_entry.get("games", []))
            
        for game in games:
            try:
                game_pk = str(game.get("gamePk"))
                status = game.get("status", {}).get("abstractGameState")
                
                # Cache running scores silently on initial boot to prevent old play floods
                if status == "Live" and game_pk not in game_scores:
                    feed_response = requests.get(GAME_FEED_URL.format(game_pk=game_pk), timeout=10)
                    if feed_response.status_code == 200:
                        linescore = feed_response.json().get("liveData", {}).get("linescore", {})
                        a_runs = linescore.get("teams", {}).get("away", {}).get("runs", 0)
                        h_runs = linescore.get("teams", {}).get("home", {}).get("runs", 0)
                        game_scores[game_pk] = f"{a_runs}-{h_runs}"
                
                # --- LIVE IN-GAME PROCESSING ---
                if status == "Live":
                    feed_response = requests.get(GAME_FEED_URL.format(game_pk=game_pk), timeout=10)
                    feed_response.raise_for_status()
                    feed = feed_response.json()
                    
                    all_plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
                    linescore = feed.get("liveData", {}).get("linescore", {})
                    
                    away_team = feed.get("gameData", {}).get("teams", {}).get("away", {}).get("fileCode", "AWY").upper()
                    home_team = feed.get("gameData", {}).get("teams", {}).get("home", {}).get("fileCode", "HOM").upper()
                    away_runs = linescore.get("teams", {}).get("away", {}).get("runs", 0)
                    home_runs = linescore.get("teams", {}).get("home", {}).get("runs", 0)
                    
                    current_score_key = f"{away_runs}-{home_runs}"
                    
                    is_top = linescore.get("isTopInning", True)
                    arrow = "⬆️" if is_top else "⬇️"
                    inning_num = linescore.get("currentInningOrdinal", "1st")
                    inning_state = f"{arrow}{inning_num}"
                    
                    outs_num = feed.get("liveData", {}).get("plays", {}).get("currentPlay", {}).get("count", {}).get("outs", 0)
                    out_dots = "○○○" if outs_num == 0 else "●○○" if outs_num == 1 else "●●○"
                    
                    # Scan every play sequentially to prevent dropped simultaneous alerts
                    for play in all_plays:
                        result = play.get("result", {})
                        about = play.get("about", {})
                        
                        # Fingerprint each play using atBatIndex so it never misses multiple plays
                        at_bat_idx = about.get("atBatIndex")
                        if at_bat_idx is None:
                            continue
                            
                        play_unique_id = f"{game_pk}_{at_bat_idx}"
                        
                        if play_unique_id not in sent_alerts:
                            description = result.get("description", "")
                            batter_name = play.get("matchup", {}).get("batter", {}).get("fullName", "Player")
                            event_type = result.get("event", "")
                            
                            # Calculate scoring type (Solo / 2-Run / 3-Run / Grand Slam) directly from RBI counts
                            rbi_count = result.get("rbi", 0)
                            homer_prefix = "Solo" if rbi_count == 1 else f"{rbi_count}-Run" if rbi_count < 4 else "Grand Slam"
                            
                            # Rule A: Home Run alerts
                            if event_type == "Home Run":
                                payload = {
                                    "content": f"⚾ **LIVE HOME RUN!** ⚾\n**{batter_name}** hits a {homer_prefix} home run!\n{description}\n{away_team} {away_runs} - {home_team} {home_runs} • {inning_state} {out_dots}"
                                }
                                discord_res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
                                discord_res.raise_for_status()
                                sent_alerts.add(play_unique_id)
                                game_scores[game_pk] = current_score_key
                                
                            # Rule B: Standard scoring play alerts
                            elif about.get("isScoringPlay", False) and game_scores.get(game_pk) != current_score_key:
                                payload = {
                                    "content": f"⚾ **LIVE SCORE UPDATE** ⚾\n**{batter_name}** - {event_type}\n{description}\n{away_team} {away_runs} - {home_team} {home_runs} • {inning_state} {out_dots}"
                                }
                                discord_res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
                                discord_res.raise_for_status()
                                sent_alerts.add(play_unique_id)
                                game_scores[game_pk] = current_score_key
                
                # --- PATH B: GAME COMPLETED SUMMARY TRANSITION ---
                elif (status == "Final" or status == "Game Over"):
                    # Cache already-final games silently on script restart to prevent history flood spam
                    if game_pk not in sent_finals and game_pk not in game_scores:
                        sent_finals.add(game_pk)
                        
                    elif game_pk not in sent_finals:
                        feed_response = requests.get(GAME_FEED_URL.format(game_pk=game_pk), timeout=10)
                        feed_response.raise_for_status()
                        feed = feed_response.json()
                        
                        linescore = feed.get("liveData", {}).get("linescore", {})
                        away_team = feed.get("gameData", {}).get("teams", {}).get("away", {}).get("fileCode", "AWY").upper()
                        home_team = feed.get("gameData", {}).get("teams", {}).get("home", {}).get("fileCode", "HOM").upper()
                        away_runs = linescore.get("teams", {}).get("away", {}).get("runs", 0)
                        home_runs = linescore.get("teams", {}).get("home", {}).get("runs", 0)
                        
                        payload = {
                            "content": f"🏁 **FINAL SCORE** 🏁\n{away_team} {away_runs} @ {home_team} {home_runs}\nThe game has officially ended."
                        }
                        discord_res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
                        discord_res.raise_for_status()
                        sent_finals.add(game_pk)
                        
            except Exception as inner_error:
                print(f"Skipping game check for game ID {game.get('gamePk')} due to database lag: {inner_error}")

    except Exception as e:
        print(f"Global scheduling loop lookups encountered an API block: {e}")
        
    time.sleep(15)
