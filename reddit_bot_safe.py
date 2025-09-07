#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reddit bot (Loufi’s Art / ArtLift) — Anti-spam safe, GitHub Actions friendly

- Max 4 posts/jour, pas de posts la nuit (23:00–07:00 Europe/Brussels)
- 2 images GM/GN max/jour (1 matin, 1 soir), 2 liens max/jour, 1 "long" (self-post) max/jour, le reste = rien (on saute) — pas de "repost" sur Reddit
- Créneaux souples (matin/midi/soir) + délais aléatoires → fluidité
- Images choisies depuis ./assets/posts et évitées si utilisées dans les 14 derniers jours
- Opt-in engagements ONLY (mentions dans la boîte de réception). Upvotes autorisés ; pas de commentaires non sollicités
- Daily + hourly caps, random delays, backoff basique
- Anti-répétition (texte 7j, images 14j)
- --oneshot pour CI | --loop pour local | --whoami pour tester l’auth

Dépendances:
  pip install praw

Variables d’environnement requises:
  REDDIT_CLIENT_ID
  REDDIT_CLIENT_SECRET
  REDDIT_USERNAME
  REDDIT_PASSWORD
  REDDIT_USER_AGENT="LoufiArtBot/1.0 by u/<ton_user>"
  REDDIT_SUBREDDITS="art,ArtistLounge"          # liste séparée par virgules
  SITE_URL="https://louphi1987.github.io/Site_de_Louphi/"
  OPENSEA_URL="https://opensea.io/collection/loufis-art"  # optionnel
  ASSETS_DIR="."

Notes:
  - Lis les règles de chaque subreddit (images autorisées ? flair requis ?).
  - Espace les posts et varie les titres.
"""

import os
import sys
import json
import time
import random
import argparse
import pathlib
import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

import praw
from praw.exceptions import APIException, RedditAPIException, ClientException, PRAWException

# ========== USER CONFIG PAR DÉFAUT ==========
TIMEZONE = "Europe/Brussels"

ASSETS_DIR = os.getenv("ASSETS_DIR", ".")
ALLOWED_EXTS = {".jpg", ".jpeg", ".png"}
IMAGE_RECENCY_DAYS = 14

# Quiet hours
NO_POST_START_HOUR = 23  # inclusive
NO_POST_END_HOUR = 7     # exclusive

# Global caps
MAX_POSTS_PER_DAY = 4
MAX_POSTS_PER_HOUR = 2

# Par type (aligné sur ton script)
MAX_IMG_GMGN_PER_DAY = 2      # 1 matin + 1 soir
MAX_SHORT_LINK_PER_DAY = 2
MAX_GMGN_LONG_PER_DAY = 1

# Delays
DELAY_POST_MIN_S = 8
DELAY_POST_MAX_S = 28
DELAY_ENGAGE_MIN_S = 12
DELAY_ENGAGE_MAX_S = 45

# Anti-répétition
TEXT_REPEAT_DAYS = 7

# Texte & liens
SITE_URL = os.getenv("SITE_URL", "").strip()
OPENSEA_URL = os.getenv("OPENSEA_URL", "").strip()

GM_SHORT = ["GM ☀️", "GM ✨", "GM 🌞", "GM 🌿", "GM 👋"]
GN_SHORT_BASE = ["GN", "Gn", "gn", "Good night", "Night"]
RANDOM_GN_EMOJIS = ["🌙", "✨", "⭐", "💤", "🌌", "🫶", "💫", "😴", "🌠"]

GM_LONG = [
    "GM 🌱 Wishing you a day full of creativity and light.",
    "GM ✨ New day, new brushstrokes.",
    "GM 🌊 Let's dive into imagination today.",
]
GN_LONG = [
    "Good night 🌙💫 May your dreams be as colorful as art.",
    "GN 🌌 See you in tomorrow’s stories.",
    "Resting the canvas for tomorrow’s colors. GN ✨",
]

LINK_POOLS = [u for u in [SITE_URL, OPENSEA_URL] if u]

COMMENT_SHORT = [
    "Thanks for the mention!",
    "Appreciate it 🙏",
    "Thanks for looping me in ✨",
    "Thanks!",
]
COMMENT_EMOJIS = ["🔥", "👍", "👏", "😍", "✨", "🫶", "🎉", "💯", "🤝", "⚡", "🌟"]

# ========== STATE ==========
STATE_FILE = "reddit_bot_state.json"

@dataclass
class DailyCounters:
    date: str
    posts: int

@dataclass
class HourlyCounters:
    hour_key: str
    posts: int

def _pertype_zero() -> Dict[str, int]:
    return {"post_img_gmgn_short": 0, "post_gmgn_long": 0, "post_short_link": 0}

def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            state.setdefault("history", [])
            state.setdefault("daily", {"date": "", "posts": 0})
            state.setdefault("hourly", {"key": "", "posts": 0})
            state.setdefault("processed_mentions", [])
            state.setdefault("pertype", _pertype_zero())
            return state
        except Exception:
            pass
    return {
        "history": [],
        "daily": {"date": "", "posts": 0},
        "hourly": {"key": "", "posts": 0},
        "processed_mentions": [],
        "pertype": _pertype_zero(),
    }

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def reset_daily_if_needed(state: Dict[str, Any], now_local: dt.datetime) -> None:
    today = now_local.date().isoformat()
    if state["daily"].get("date") != today:
        state["daily"] = {"date": today, "posts": 0}
        state["pertype"] = _pertype_zero()

def reset_hourly_if_needed(state: Dict[str, Any], now_local: dt.datetime) -> None:
    key = f"{now_local.date().isoformat()}_{now_local.hour:02d}"
    if state["hourly"].get("key") != key:
        state["hourly"] = {"key": key, "posts": 0}

def remember_post(state: Dict[str, Any], text: str, media: Optional[str] = None) -> None:
    now = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    rec = {"text": text, "ts": now}
    if media:
        rec["media"] = media
    state["history"].append(rec)
    state["history"] = state["history"][-400:]

def recently_used_text(state: Dict[str, Any], text: str, days: int = TEXT_REPEAT_DAYS) -> bool:
    cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=days)
    for item in reversed(state.get("history", [])):
        ts = item.get("ts")
        if not ts:
            continue
        try:
            when = dt.datetime.fromisoformat(ts)
        except Exception:
            continue
        if when >= cutoff and item.get("text", "").strip() == text.strip():
            return True
    return False

def recently_used_media(state: Dict[str, Any], media_path: str, days: int = IMAGE_RECENCY_DAYS) -> bool:
    cutoff = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=days)
    for item in reversed(state.get("history", [])):
        ts = item.get("ts")
        mp = item.get("media")
        if not ts or not mp:
            continue
        try:
            when = dt.datetime.fromisoformat(ts)
        except Exception:
            continue
        if when >= cutoff and mp == media_path:
            return True
    return False

# ========== FILES / IMAGES ==========

def list_local_images(folder: str) -> List[str]:
    p = pathlib.Path(folder)
    if not p.exists():
        return []
    return [str(f) for f in p.iterdir() if f.is_file() and f.suffix.lower() in ALLOWED_EXTS]

def pick_fresh_image(state: Dict[str, Any]) -> Optional[str]:
    imgs = list_local_images(ASSETS_DIR)
    if not imgs:
        return None
    random.shuffle(imgs)
    for img in imgs:
        if not recently_used_media(state, img, days=IMAGE_RECENCY_DAYS):
            return img
    return random.choice(imgs)

# ========== REDDIT CLIENT & BACKOFF ==========

def reddit_client() -> praw.Reddit:
    # Password grant (simple pour CI). Si tu veux passer en refresh-token, je peux te l'ajouter.
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=os.environ.get("REDDIT_USER_AGENT", "LoufiArtBot/1.0"),
        ratelimit_seconds=5,
    )

def with_backoff(fn):
    def wrapper(*args, **kwargs):
        delay = 4.0
        tries = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except (APIException, RedditAPIException, ClientException, PRAWException, Exception) as e:
                tries += 1
                # Backoff simple
                sleep_s = min(delay * (2 ** (tries - 1)), 60.0)
                print(f"[BACKOFF] {e} → sleep {int(sleep_s)}s", file=sys.stderr)
                time.sleep(sleep_s)
                if tries >= 5:
                    raise
    return wrapper

# ========== CONTENT PICKERS ==========

def in_time_window(now_local: dt.datetime, window: str) -> bool:
    h = now_local.hour
    if window == "morning":
        return 7 <= h < 11
    if window == "evening":
        return 19 <= h < 23
    if window == "midday":
        return 11 <= h < 19
    return False

def is_quiet_hours(now_local: dt.datetime) -> bool:
    h = now_local.hour
    if NO_POST_START_HOUR <= NO_POST_END_HOUR:
        return NO_POST_START_HOUR <= h < NO_POST_END_HOUR
    return h >= NO_POST_START_HOUR or h < NO_POST_END_HOUR

def pick_without_recent(state: Dict[str, Any], pool: List[str]) -> str:
    shuffled = pool[:]
    random.shuffle(shuffled)
    for s in shuffled:
        if not recently_used_text(state, s):
            return s
    return random.choice(pool)

def build_gm_short() -> str:
    return random.choice(GM_SHORT)

def build_gn_short() -> str:
    base = random.choice(GN_SHORT_BASE)
    if random.random() < 0.85:
        base = f"{base} {random.choice(RANDOM_GN_EMOJIS)}"
    return base

def pick_gmgn_text(state: Dict[str, Any], now_local: dt.datetime, long: bool = False) -> str:
    if in_time_window(now_local, "morning"):
        return pick_without_recent(state, GM_LONG) if long else build_gm_short()
    if in_time_window(now_local, "evening"):
        return pick_without_recent(state, GN_LONG) if long else build_gn_short()
    return build_gm_short()

def pick_link_short(state: Dict[str, Any]) -> Optional[str]:
    pools = LINK_POOLS[:]
    random.shuffle(pools)
    for url in pools:
        if not recently_used_text(state, url):
            return url
    return pools[0] if pools else None

# ========== ACTION SELECTION ==========
def choose_action_with_caps(now_local: dt.datetime, pertype: Dict[str, int]) -> str:
    h = now_local.hour
    if 7 <= h < 11:
        if pertype["post_img_gmgn_short"] < MAX_IMG_GMGN_PER_DAY and pertype["post_img_gmgn_short"] == 0:
            return "post_img_gmgn_short"
        if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
            return "post_short_link"
        if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
            return "post_gmgn_long"
        return "skip"

    if 11 <= h < 19:
        if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
            return "post_short_link"
        if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
            return "post_gmgn_long"
        return "skip"

    if 19 <= h < 23:
        if pertype["post_img_gmgn_short"] < MAX_IMG_GMGN_PER_DAY:
            return "post_img_gmgn_short"
        if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
            return "post_short_link"
        if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
            return "post_gmgn_long"
        return "skip"

    if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
        return "post_short_link"
    if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
        return "post_gmgn_long"
    return "skip"

# ========== ENGINE UTILS ==========
def can_post(state: Dict[str, Any]) -> bool:
    return state["daily"]["posts"] < MAX_POSTS_PER_DAY and state["hourly"]["posts"] < MAX_POSTS_PER_HOUR

# ========== ENGAGEMENTS OPT-IN (mentions) ==========
@with_backoff
def fetch_unprocessed_mentions(r: praw.Reddit, state: Dict[str, Any], limit: int = 25):
    fresh = []
    processed = set(state.get("processed_mentions", []))
    # Mentions = inbox mentions / commentaires te taggant
    for item in r.inbox.mentions(limit=limit):
        mid = item.fullname
        if mid not in processed:
            fresh.append(item)
    return fresh

@with_backoff
def engage_for_mention(item) -> Optional[str]:
    # Upvote ou bref merci (75/25)
    try:
        if random.random() < 0.75:
            item.upvote()
            return "upvote"
        else:
            reply = random.choice(COMMENT_SHORT) if random.random() < 0.7 else random.choice(COMMENT_EMOJIS)
            item.reply(reply)
            return f"reply:{reply}"
    except Exception as e:
        print(f"[engage] {e}", file=sys.stderr)
        return None

# ========== POST ACTIONS ==========
@with_backoff
def submit_link(sub, title: str, url: str):
    return sub.submit(title=title, url=url, resubmit=False, send_replies=False)

@with_backoff
def submit_image(sub, title: str, image_path: str):
    # Certains subs refusent les images; si ça plante, on catch côté appelant.
    return sub.submit_image(title=title, image_path=image_path, send_replies=False)

@with_backoff
def submit_selfpost(sub, title: str, body: str):
    return sub.submit(title=title, selftext=body, send_replies=False)

# ========== MAIN ACTION ==========
def do_one_action(r: praw.Reddit, state: Dict[str, Any], tz: ZoneInfo) -> str:
    now_local = dt.datetime.now(tz)
    reset_daily_if_needed(state, now_local)
    reset_hourly_if_needed(state, now_local)

    # 1) Mentions opt-in (engagement léger)
    try:
        mentions = fetch_unprocessed_mentions(r, state, limit=25)
        random.shuffle(mentions)
        if mentions:
            item = mentions[0]
            kind = engage_for_mention(item)
            if kind:
                mid = item.fullname
                state.setdefault("processed_mentions", []).append(mid)
                state["processed_mentions"] = state["processed_mentions"][-500:]
                save_state(state)
                nap = random.uniform(DELAY_ENGAGE_MIN_S, DELAY_ENGAGE_MAX_S)
                print(f"Engaged ({kind}). Sleeping ~{int(nap)}s...")
                time.sleep(nap)
                return "engaged"
    except Exception as e:
        print(f"[mentions] {e}", file=sys.stderr)

    # 2) Posting
    if not can_post(state) or is_quiet_hours(now_local):
        print("Nothing to do (caps reached / quiet hours)")
        return "skip"

    pertype = state.get("pertype", _pertype_zero())
    action = choose_action_with_caps(now_local, pertype)

    if action == "skip":
        print("No suitable action under caps → skip")
        return "skip"

    # Choix subreddit
    subs_env = os.getenv("REDDIT_SUBREDDITS", "")
    subs = [s.strip() for s in subs_env.split(",") if s.strip()]
    if not subs:
        print("No subreddits configured in REDDIT_SUBREDDITS → skip")
        return "skip"
    sub_name = random.choice(subs)
    sub = r.subreddit(sub_name)

    title_choices = [
        "Small art drop ✨",
        "Sharing a piece from Loufi’s Art 🎨",
        "A little color for your day 🌈",
        "Art moment ✨",
        "Color break 🎨",
    ]
    title = random.choice(title_choices)

    text: Optional[str] = None
    image: Optional[str] = None
    url: Optional[str] = None

    # Préparation
    if action == "post_img_gmgn_short":
        text = pick_gmgn_text(state, now_local, long=False)
        image = pick_fresh_image(state)
        if image is None:
            # downgrade
            if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
                action = "post_short_link"
            elif pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
                action = "post_gmgn_long"
            else:
                return "skip"

    if action == "post_gmgn_long":
        if pertype["post_gmgn_long"] >= MAX_GMGN_LONG_PER_DAY:
            if pertype["post_short_link"] < MAX_SHORT_LINK_PER_DAY:
                action = "post_short_link"
            else:
                return "skip"
        else:
            body = pick_gmgn_text(state, now_local, long=True)
            # On met aussi un lien en fin de selfpost pour le contexte (si dispo)
            if LINK_POOLS:
                body += f"\n\n—\nMore: {random.choice(LINK_POOLS)}"
            text = body

    if action == "post_short_link":
        if pertype["post_short_link"] >= MAX_SHORT_LINK_PER_DAY:
            if pertype["post_gmgn_long"] < MAX_GMGN_LONG_PER_DAY:
                action = "post_gmgn_long"
            else:
                return "skip"
        else:
            url = pick_link_short(state)
            if not url:
                # pas de liens configurés → fallback selfpost court
                action = "post_gmgn_long"
                text = pick_gmgn_text(state, now_local, long=True)

    # Exécution
    try:
        if action == "post_img_gmgn_short" and image:
            print(f"[image] r/{sub_name} ← {pathlib.Path(image).name}")
            submit_image(sub, title=title, image_path=image)
            remember_post(state, title, media=image)
        elif action == "post_gmgn_long" and text:
            print(f"[self] r/{sub_name}")
            submit_selfpost(sub, title=title, body=text)
            remember_post(state, text)
        elif action == "post_short_link" and url:
            print(f"[link] r/{sub_name} ← {url}")
            submit_link(sub, title=title, url=url)
            remember_post(state, url)
        else:
            print("Nothing prepared → skip")
            return "skip"
    except Exception as e:
        print(f"[post] error: {e}", file=sys.stderr)
        return "post_failed"

    # Mise à jour des compteurs
    state["daily"]["posts"] += 1
    state["hourly"]["posts"] += 1
    if action in state["pertype"]:
        state["pertype"][action] += 1
    save_state(state)

    # Dodo aléatoire
    nap = random.uniform(DELAY_POST_MIN_S, DELAY_POST_MAX_S)
    print(f"Posted ({action}). Sleeping ~{int(nap)}s…")
    time.sleep(nap)
    return "posted"

# ========== CLI ==========
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oneshot", action="store_true", help="Perform one safe action and exit (CI mode)")
    ap.add_argument("--loop", action="store_true", help="Run continuous loop with sleeps (local use)")
    ap.add_argument("--whoami", action="store_true", help="Print the authenticated user and exit")
    args = ap.parse_args()

    tz = ZoneInfo(TIMEZONE)
    r = reddit_client()
    state = load_state()

    if args.whoami:
        me = r.user.me()
        print(f"Authenticated as: u/{me.name}")
        return

    if args.oneshot or not args.loop:
        status = do_one_action(r, state, tz)
        print(f"Status: {status}")
        sys.exit(0)

    print("Loop mode (anti-spam). Ctrl+C to stop.")
    while True:
        try:
            do_one_action(r, state, tz)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            cool = random.uniform(60, 120)
            print(f"[Loop warn] {e}. Cooling down {int(cool)}s", file=sys.stderr)
            time.sleep(cool)
        now_local = dt.datetime.now(tz)
        if is_quiet_hours(now_local):
            nap = random.uniform(70*60, 120*60)
        elif 7 <= now_local.hour < 23:
            nap = random.uniform(25*60, 55*60)
        else:
            nap = random.uniform(45*60, 80*60)
        if random.random() < 0.18:
            nap += random.uniform(20*60, 40*60)
        print(f"Sleeping ~{int(nap//60)} min…")
        time.sleep(nap)

if __name__ == "__main__":
    main()
