"""Parse Twitter archive JS files into structured tweet data."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator


@dataclass
class Tweet:
    """Parsed tweet data."""
    id: str
    full_text: str
    created_at: datetime
    favorite_count: int
    retweet_count: int
    is_reply: bool
    is_retweet: bool
    is_quote: bool
    hashtags: list[str]
    urls: list[str]
    mentions: list[str]
    raw_json: dict

    @property
    def char_count(self) -> int:
        return len(self.full_text)

    @property
    def is_original(self) -> bool:
        """Check if this is original content (not RT, not short reply)."""
        if self.is_retweet:
            return False
        if self.is_reply and self.char_count < 50:
            return False
        return True


def parse_js_file(file_path: Path) -> list[dict]:
    """Parse a Twitter archive JS file into JSON data.

    Twitter archive files start with 'window.YTD.*.part* = ' followed by JSON.
    """
    content = file_path.read_text(encoding="utf-8")

    # Strip the JS variable assignment prefix
    match = re.match(r"window\.YTD\.\w+\.part\d+\s*=\s*", content)
    if match:
        content = content[match.end():]

    return json.loads(content)


def parse_tweet_date(date_str: str) -> datetime:
    """Parse Twitter's date format: 'Wed Dec 31 19:12:36 +0000 2025'"""
    return datetime.strptime(date_str, "%a %b %d %H:%M:%S %z %Y")


def extract_tweet(data: dict) -> Tweet:
    """Extract a Tweet object from raw archive data."""
    tweet_data = data.get("tweet", data)

    # Detect tweet type
    full_text = tweet_data.get("full_text", "")
    is_retweet = full_text.startswith("RT @")
    is_reply = tweet_data.get("in_reply_to_status_id") is not None
    is_quote = "quoted_status_id" in tweet_data

    # Extract entities
    entities = tweet_data.get("entities", {})
    hashtags = [h["text"] for h in entities.get("hashtags", [])]
    urls = [u["expanded_url"] for u in entities.get("urls", []) if "expanded_url" in u]
    mentions = [m["screen_name"] for m in entities.get("user_mentions", [])]

    return Tweet(
        id=tweet_data["id_str"],
        full_text=full_text,
        created_at=parse_tweet_date(tweet_data["created_at"]),
        favorite_count=int(tweet_data.get("favorite_count", 0)),
        retweet_count=int(tweet_data.get("retweet_count", 0)),
        is_reply=is_reply,
        is_retweet=is_retweet,
        is_quote=is_quote,
        hashtags=hashtags,
        urls=urls,
        mentions=mentions,
        raw_json=tweet_data,
    )


def load_note_tweets(archive_path: Path) -> dict[str, str]:
    """Load long-form tweet content from note-tweet.js.

    Returns a mapping of tweet_id -> full text for tweets > 280 chars.
    """
    note_file = archive_path / "data" / "note-tweet.js"
    if not note_file.exists():
        return {}

    notes = {}
    data = parse_js_file(note_file)
    for item in data:
        note = item.get("noteTweet", {})
        note_id = note.get("noteTweetId")
        text = note.get("core", {}).get("text", "")
        if note_id and text:
            notes[note_id] = text

    return notes


def iter_tweets(archive_path: Path, include_rts: bool = False) -> Iterator[Tweet]:
    """Iterate over all tweets in the archive.

    Args:
        archive_path: Path to the Twitter archive root folder
        include_rts: Whether to include retweets (default: False)

    Yields:
        Tweet objects
    """
    data_path = archive_path / "data"

    # Load note tweets for long-form content
    note_tweets = load_note_tweets(archive_path)

    # Find all tweet files (tweets.js, tweets-part1.js, etc.)
    tweet_files = sorted(data_path.glob("tweets*.js"))

    seen_ids = set()

    for tweet_file in tweet_files:
        data = parse_js_file(tweet_file)

        for item in data:
            tweet = extract_tweet(item)

            # Skip duplicates
            if tweet.id in seen_ids:
                continue
            seen_ids.add(tweet.id)

            # Skip retweets if not wanted
            if not include_rts and tweet.is_retweet:
                continue

            # Replace truncated text with full note content if available
            if tweet.id in note_tweets:
                tweet.full_text = note_tweets[tweet.id]

            yield tweet


def get_archive_stats(archive_path: Path) -> dict:
    """Get basic statistics about the archive."""
    total = 0
    retweets = 0
    replies = 0
    original = 0

    for tweet in iter_tweets(archive_path, include_rts=True):
        total += 1
        if tweet.is_retweet:
            retweets += 1
        elif tweet.is_reply:
            replies += 1
        else:
            original += 1

    return {
        "total": total,
        "retweets": retweets,
        "replies": replies,
        "original": original,
    }
