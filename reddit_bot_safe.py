#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reddit bot (Loufi’s Art / ArtLift) — Anti-spam safe, GitHub Actions friendly

- Max 1 post / semaine (image seulement, dans ./assets/posts)
- Pas de posts la nuit (23:00–07:00 Europe/Brussels)
- Upvotes quotidiens (aléatoires, sur les subreddits configurés)
- Délais aléatoires pour fluidité
- Anti-répétition : images non repostées avant 14j
- Opt-in uniquement (pas de commentaires auto)

Dépendances:
  pip install praw

Secrets/variables nécessaires:
  REDDIT_CLIENT_ID
  REDDIT_CLIENT_SECRET
  REDDIT_USERNAME
  REDDIT_PASSWORD
  REDDIT_USER_AGENT="LoufiArtBot/1.0 by u/<ton_user>"
  REDDIT_SUBREDDITS="art,ArtistLounge"
  ASSETS_DIR="."
"""

import os, sys, json, time, random, argparse, pathlib, datetime as dt
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

import praw
from praw.exceptions import APIException, RedditAPIException, ClientException, PRAWException

# Config
TIMEZONE = "Europe/Brussels"
STATE_FILE = "reddit_bot_state.json"
ALLOWED_EXTS = {".jpg", ".jpeg", ".png"}
IMAGE_RECENCY_DAYS = 14

# Quiet hours
NO_POST_START_HOUR = 23
NO_POST_END_HOUR = 7

# Caps
MAX_POSTS_PER_WEEK = 1
MAX_UPVOTES_PER_DAY = 5

# Delays
DELAY_MIN_S, DELAY_MAX_S = 8, 25

# ========= STATE ==========
def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, "r", encoding="utf-8"))
        except Exception:
            pass
    return {
        "weekly": {"week": "", "posts": 0},
        "daily": {"date": "", "upvotes": 0},
        "history": [],
    }

def save_state(state: Dict[str, Any]) -> None:
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def reset_counters(state: Dict[str, Any], now: dt.datetime):
    # Reset daily upvotes
    today = now.date().isoformat()
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "upvotes": 0}
    # Reset weekly posts
    year, week, _ = now.isocalendar()
    week_key = f"{year}-W{week:02d}"
    if state["weekly"].get("week") != week_key:
        state["weekly"] = {"week": week_key, "posts": 0}

def remember_post(state: Dict[str, Any], media: str):
    state["history"].append({"media": media, "ts": dt.datetime.now(dt.timezone.utc).isoformat()})
    state["history"] = state["history"][-200:]

def recently_used_media(state: Dict[str, Any], media: str, days: int = IMAGE_RECENCY_DAYS) -> bool:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    for rec in reversed(state.get("history", [])):
        if not rec.get("ts") or not rec.get("media"):
            continue
        try:
            when = dt.datetime.fromisoformat(rec["ts"])
        except Exception:
            continue
        if when >= cutoff and rec["media"] == media:
            return True
    return False

# ========= REDDIT ==========
def reddit_client() -> praw.Reddit:
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=os.environ.get("REDDIT_USER_AGENT", "LoufiArtBot/1.0"),
    )

def with_backoff(fn):
    def wrapper(*args, **kwargs):
        tries, delay = 0, 5
        while True:
            try:
                return fn(*args, **kwargs)
            except (APIException, RedditAPIException, ClientException, PRAWException, Exception) as e:
                tries += 1
                sleep_s = min(delay * (2 ** (tries - 1)), 60)
                print(f"[backoff] {e} → sleep {sleep_s}s", file=sys.stderr)
                time.sleep(sleep_s)
                if tries > 4:
                    raise
    return wrapper

# ========= ACTIONS ==========
def list_local_images(folder: str) -> List[str]:
    p = pathlib.Path(folder)
    return [str(f) for f in p.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_EXTS] if p.exists() else []

def pick_fresh_image(state: Dict[str, Any], folder: str) -> Optional[str]:
    imgs = list_local_images(folder)
    random.shuffle(imgs)
    for img in imgs:
        if not recently_used_media(state, img):
            return img
    return random.choice(imgs) if imgs else None

@with_backoff
def submit_image(sub, title: str, image_path: str):
    return sub.submit_image(title=title, image_path=image_path, send_replies=False)

def do_weekly_post(r: praw.Reddit, state: Dict[str, Any], tz: ZoneInfo) -> str:
    now = dt.datetime.now(tz)
    reset_counters(state, now)
    if state["weekly"]["posts"] >= MAX_POSTS_PER_WEEK:
        return "skip_weekly_cap"
    if NO_POST_START_HOUR <= now.hour < NO_POST_END_HOUR:
        return "skip_quiet_hours"

    subs = [s.strip() for s in os.getenv("REDDIT_SUBREDDITS", "").split(",") if s.strip()]
    if not subs:
        return "no_subs"
    sub = r.subreddit(random.choice(subs))

    img = pick_fresh_image(state, os.getenv("ASSETS_DIR", "."))
    if not img:
        return "no_image"

    title = random.choice([
        "Sharing a piece from Loufi’s Art 🎨",
        "A little color for your day 🌈",
        "Art drop ✨",
    ])
    print(f"[post] r/{sub.display_name} ← {pathlib.Path(img).name}")
    submit_image(sub, title=title, image_path=img)
    remember_post(state, img)
    state["weekly"]["posts"] += 1
    save_state(state)
    time.sleep(random.uniform(DELAY_MIN_S, DELAY_MAX_S))
    return "posted"

def do_daily_upvotes(r: praw.Reddit, state: Dict[str, Any], tz: ZoneInfo) -> str:
    now = dt.datetime.now(tz)
    reset_counters(state, now)
    if state["daily"]["upvotes"] >= MAX_UPVOTES_PER_DAY:
        return "skip_upvotes_cap"

    subs = [s.strip() for s in os.getenv("REDDIT_SUBREDDITS", "").split(",") if s.strip()]
    if not subs:
        return "no_subs"
    sub = r.subreddit(random.choice(subs))

    count = random.randint(1, 3)
    for post in sub.hot(limit=20):
        if random.random() < 0.3:
            try:
                post.upvote()
                state["daily"]["upvotes"] += 1
                print(f"[upvote] {post.title[:40]}…")
                if state["daily"]["upvotes"] >= MAX_UPVOTES_PER_DAY:
                    break
            except Exception:
                pass
    save_state(state)
    return "upvoted"

# ========= CLI ==========
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oneshot", action="store_true", help="One action (post or upvote) and exit")
    ap.add_argument("--loop", action="store_true", help="Loop mode (local use)")
    ap.add_argument("--whoami", action="store_true", help="Test authentication")
    args = ap.parse_args()

    tz = ZoneInfo(TIMEZONE)
    r = reddit_client()
    state = load_state()

    if args.whoami:
        me = r.user.me()
        print(f"Authenticated as: u/{me.name}")
        return

    def run_once():
        if random.random() < 0.2:  # ~20% chance to try posting
            print(do_weekly_post(r, state, tz))
        else:
            print(do_daily_upvotes(r, state, tz))

    if args.oneshot or not args.loop:
        run_once()
        return

    print("Loop mode. Ctrl+C to stop.")
    while True:
        run_once()
        nap = random.uniform(25*60, 55*60)
        print(f"Sleeping ~{int(nap//60)} min")
        time.sleep(nap)

if __name__ == "__main__":
    main()
