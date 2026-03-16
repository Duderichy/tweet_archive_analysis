"""Claude API integration for analyzing tweets."""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import anthropic
from dotenv import load_dotenv

from .database import Database, TweetAnalysis

load_dotenv()

# Patterns that indicate low-insight tweets
LOW_VALUE_PATTERNS = re.compile(
    r'^(lol|lmao|omg|same|this|mood|facts|fr|ngl|tbh|ikr|yep|yup|nope|wow|oof|rip|'
    r'haha|hehe|yes|no|ok|okay|sure|true|real|based|dead|crying|screaming|i cant|'
    r'me too|big if true|ratio|w|l|huge|wild|insane|crazy|literally me|'
    r'@\w+\s*(lol|lmao|haha|yes|no|true|same|this|mood|dead|crying|fr|facts))\.?\s*$',
    re.IGNORECASE
)


def is_low_value_tweet(text: str) -> bool:
    """Check if tweet is likely low-value (conversational, reactions, etc.)."""
    # Remove URLs for analysis
    text_no_urls = re.sub(r'https?://\S+', '', text).strip()

    # Skip if mostly empty after removing URLs
    if len(text_no_urls) < 20:
        return True

    # Skip if >60% is @mentions
    mentions = re.findall(r'@\w+', text_no_urls)
    mention_chars = sum(len(m) for m in mentions)
    if mention_chars > len(text_no_urls) * 0.6:
        return True

    # Skip conversational patterns
    if LOW_VALUE_PATTERNS.match(text_no_urls):
        return True

    # Skip if it's just a quote tweet with no commentary
    if text_no_urls.startswith('RT @') or text_no_urls == '':
        return True

    return False

# Fast screening prompt for Haiku (compressed)
SCREEN_PROMPT = """Score these tweets 0-10 for insight/blog potential. Return JSON array:
[{{"id":"...","score":N}}]

Tweets:
{tweets}

JSON only:"""

# Detailed analysis prompt for Sonnet (compressed)
ANALYSIS_PROMPT = """Analyze tweets for blog potential. Return JSON array:
[{{"tweet_id":"...","insight_score":N,"categories":["cat1"],"summary":"...","blog_potential":"high|medium|low","key_ideas":["idea1"]}}]

Categories: productivity,technology,philosophy,career,creativity,psychology,business,life-advice,observations,humor,economics,health,culture

Tweets:
{tweets}

JSON only:"""


@dataclass
class AnalyzerConfig:
    """Configuration for the analyzer."""
    screen_batch_size: int = 75  # Haiku struggles with larger batches
    detail_batch_size: int = 30
    parallel_workers: int = 2
    screen_model: str = "claude-3-haiku-20240307"
    detail_model: str = "claude-sonnet-4-20250514"
    screen_threshold: float = 8.0
    min_likes: int = 10  # Only analyze tweets with this many likes


class TweetAnalyzer:
    """Analyze tweets using Claude API with two-pass filtering."""

    def __init__(self, db: Database, config: AnalyzerConfig | None = None):
        self.db = db
        self.config = config or AnalyzerConfig()
        self.client = anthropic.Anthropic()

    def _parse_json_response(self, content: str) -> list:
        """Parse JSON from response, handling markdown code blocks."""
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        return json.loads(content)

    def _screen_batch(self, tweets: list[dict]) -> tuple[list[dict], int]:
        """Quick screen a batch with Haiku, save all scores, return high scorers."""
        tweet_text = "\n".join(f"[{t['id']}] {t['full_text'][:300]}" for t in tweets)

        response = self.client.messages.create(
            model=self.config.screen_model,
            max_tokens=2048,
            messages=[{"role": "user", "content": SCREEN_PROMPT.format(tweets=tweet_text)}],
        )

        results = self._parse_json_response(response.content[0].text)

        # Save ALL screening results to cache
        scores = {}
        for r in results:
            tid = r.get("id")
            score = r.get("score", 0)
            if tid:
                scores[tid] = score
                self.db.insert_screening(tid, score)

        high_scorers = [t for t in tweets if scores.get(t["id"], 0) >= self.config.screen_threshold]
        return high_scorers, len(tweets)

    def _analyze_batch(self, tweets: list[dict]) -> list[TweetAnalysis]:
        """Detailed analysis of pre-screened tweets with Sonnet."""
        tweet_text = "\n\n".join(f"[{t['id']}]\n{t['full_text']}" for t in tweets)

        response = self.client.messages.create(
            model=self.config.detail_model,
            max_tokens=4096,
            messages=[{"role": "user", "content": ANALYSIS_PROMPT.format(tweets=tweet_text)}],
        )

        results = self._parse_json_response(response.content[0].text)

        analyses = []
        for result in results:
            analysis = TweetAnalysis(
                tweet_id=result["tweet_id"],
                insight_score=float(result["insight_score"]),
                categories=result.get("categories", []),
                summary=result.get("summary", ""),
                blog_potential=result.get("blog_potential", "low"),
                key_ideas=result.get("key_ideas", []),
                analyzed_at=datetime.now(),
            )
            analyses.append(analysis)

        return analyses

    def _process_screen_batch(self, batch: list[dict], batch_num: int) -> tuple[int, list[dict], int]:
        """Process a screening batch, return (batch_num, high_scoring_tweets, total_screened)."""
        time.sleep(batch_num * 0.5)  # Stagger requests
        try:
            high_scorers, screened_count = self._screen_batch(batch)
            return (batch_num, high_scorers, screened_count)
        except (json.JSONDecodeError, anthropic.APIError) as e:
            print(f"Screen batch {batch_num} error: {e}")
            return (batch_num, [], 0)

    def _process_detail_batch(self, batch: list[dict], batch_num: int) -> tuple[int, list[TweetAnalysis]]:
        """Process a detail analysis batch, return (batch_num, analyses)."""
        time.sleep(batch_num * 1.0)  # Stagger requests (Sonnet needs more spacing)
        try:
            analyses = self._analyze_batch(batch)
            return (batch_num, analyses)
        except (json.JSONDecodeError, anthropic.APIError) as e:
            print(f"Detail batch {batch_num} error: {e}")
            return (batch_num, [])

    def analyze_tweets(
        self,
        limit: int | None = None,
        min_length: int = 30,
        min_likes: int | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Analyze tweets with two-pass filtering and caching.

        Args:
            limit: Maximum number of tweets to screen
            min_length: Minimum tweet length (default 30)
            min_likes: Minimum likes threshold (default from config)
            progress_callback: Called with (processed, total, phase)
            dry_run: If True, just return counts

        Returns:
            Dict with cached, screened, passed_filter, analyzed counts
        """
        if min_likes is None:
            min_likes = self.config.min_likes

        # Get tweets already screened that passed but weren't analyzed yet
        cached_high = self.db.get_screened_unanalyzed(threshold=self.config.screen_threshold, limit=limit)

        # Get tweets that need screening
        tweets_raw = self.db.get_unscreened_tweets(min_length=min_length, min_likes=min_likes, limit=limit)

        # Local pre-filtering (free, no API)
        tweets_to_screen = [t for t in tweets_raw if not is_low_value_tweet(t['full_text'])]
        filtered_out = len(tweets_raw) - len(tweets_to_screen)

        if dry_run:
            return {
                "cached_high": len(cached_high),
                "to_screen": len(tweets_to_screen),
                "pre_filtered": filtered_out,
                "estimated_pass": len(tweets_to_screen) // 8,
            }

        if len(tweets_to_screen) == 0 and len(cached_high) == 0:
            return {"pre_filtered": filtered_out, "cached": 0, "screened": 0, "passed_filter": 0, "analyzed": 0}

        # Phase 1: Screen unscreened tweets with Haiku (skip if none)
        new_high_scorers = []
        screened = 0

        if tweets_to_screen:
            screen_batches = [tweets_to_screen[i:i + self.config.screen_batch_size]
                             for i in range(0, len(tweets_to_screen), self.config.screen_batch_size)]

            with ThreadPoolExecutor(max_workers=self.config.parallel_workers) as executor:
                futures = {executor.submit(self._process_screen_batch, batch, i): i
                          for i, batch in enumerate(screen_batches)}

                for future in as_completed(futures):
                    batch_num, batch_high, batch_screened = future.result()
                    new_high_scorers.extend(batch_high)
                    screened += batch_screened
                    if progress_callback:
                        progress_callback(screened, len(tweets_to_screen), "screening")

        # Combine cached high scorers + newly screened high scorers
        all_high_scorers = cached_high + new_high_scorers

        if not all_high_scorers:
            return {"pre_filtered": filtered_out, "cached": len(cached_high), "screened": screened, "passed_filter": 0, "analyzed": 0}

        # Phase 2: Detailed analysis with Sonnet
        detail_batches = [all_high_scorers[i:i + self.config.detail_batch_size]
                         for i in range(0, len(all_high_scorers), self.config.detail_batch_size)]

        analyzed = 0
        detail_total = len(all_high_scorers)

        with ThreadPoolExecutor(max_workers=self.config.parallel_workers) as executor:
            futures = {executor.submit(self._process_detail_batch, batch, i): i
                      for i, batch in enumerate(detail_batches)}

            for future in as_completed(futures):
                batch_num, analyses = future.result()
                for analysis in analyses:
                    self.db.insert_analysis(analysis)
                    analyzed += 1
                if progress_callback:
                    progress_callback(analyzed, detail_total, "analyzing")

        return {
            "pre_filtered": filtered_out,
            "cached": len(cached_high),
            "screened": screened,
            "passed_filter": len(all_high_scorers),
            "analyzed": analyzed,
        }
