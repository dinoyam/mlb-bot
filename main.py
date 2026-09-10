import os
import time
from datetime import datetime
import pytz
import requests
from threading import Thread
from http.server import SimpleHTTPRequestHandler, HTTPServer

# 1. Fake Web Server to prevent Render from ever timing out or crashing
def run_fake_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHTTPRequestHandler)
    print("🌐 Port 10000 successfully bound. Render web check satisfied!")
    server.serve_forever()

# Start the fake server in a separate background thread instantly
Thread(target=run_fake_server, daemon=True).start()

# 2. Your actual core MLB tracking logic begins here
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
SCHEDULE_URL = "https://mlb.com{date}"
GAME_FEED_URL = "https://mlb.com{game_pk}/feed/live"

sent_alerts = set()
sent_finals = set()
game_scores = {}

print("⚾ League-Wide MLB Ticker is running live 24/7...")

while True:
    try:
        tz = pytz.timezone('America/New_York')
        current_date = datetime.now(tz).strftime('%Y-%m-%d')
        
        schedule_data = requests.get(SCHEDULE_URL.format(date=current_date)).json()
        games = schedule_data.get("dates", [{}]).get("games", [])
        
        for game in games:
            game_pk = str(game.get("gamePk"))
            status = game.get("status", {}).get("abstractGameState")
            
            if status == "Live":
                feed = requests.get(GAME_FEED_URL.format(game_pk=game_pk)).json()
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
                
                for play in reversed(all_plays):
                    result = play.get("result", {})
                    about = play.get("about", {})
                    play_id = f"{game_pk}_{about.get('playId')}"
                    
                    if play_id not in sent_alerts:
                        description = result.get("description", "")
                        batter_name = play.get("matchup", {}).get("batter", {}).get("fullName", "Player")
                        event_type = result.get("event", "")
                        
                        if event_type == "Home Run":
                            payload = {
                                "content": f"⚾ **LIVE HOME RUN!** ⚾\n**{batter_name}** - {result.get('detailedDescription', 'Home Run')}\n{description}\n{away_team} {away_runs} - {home_team} {home_runs} • {inning_state} {out_dots}"
                            }
                            requests.post(DISCORD_WEBHOOK_URL, json=payload)
                            sent_alerts.add(play_id)
                            game_scores[game_pk] = current_score_key
                            break
                        
                        elif about.get("isScoringPlay", False) and game_scores.get(game_pk) != current_score_key:
                            payload = {
                                "content": f"⚾ **LIVE SCORE UPDATE** ⚾\n**{batter_name}** - {event_type}\n{description}\n{away_team} {away_runs} - {home_team} {home_runs} • {inning_state} {out_dots}"
                            }
                            requests.post(DISCORD_WEBHOOK_URL, json=payload)
                            sent_alerts.add(play_id)
                            game_scores[game_pk] = current_score_key
                            break
                            
                if game_pk not in game_scores:
                    game_scores[game_pk] = current_score_key

            elif (status == "Final" or status == "Game Over") and game_pk not in sent_finals:
                feed = requests.get(GAME_FEED_URL.format(game_pk=game_pk)).json()
                linescore = feed.get("liveData", {}).get("linescore", {})
                away_team = feed.get("gameData", {}).get("teams", {}).get("away", {}).get("fileCode", "AWY").upper()
                home_team = feed.get("gameData", {}).get("teams", {}).get("home", {}).get("fileCode", "HOM").upper()
                away_runs = linescore.get("teams", {}).get("away", {}).get("runs", 0)
                home_runs = linescore.get("teams", {}).get("home", {}).get("runs", 0)
                
                payload = {
                    "content": f"🏁 **FINAL SCORE** 🏁\n{away_team} {away_runs} @ {home_team} {home_runs}\nThe game has officially ended."
                }
                requests.post(DISCORD_WEBHOOK_URL, json=payload)
                sent_finals.add(game_pk)

    except Exception as e:
        print(f"Loop check alert delay: {e}")
        
    time.sleep(15)
