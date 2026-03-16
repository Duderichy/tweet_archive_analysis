"""SQLite database layer for storing tweets and analysis results."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .parser import Tweet


@dataclass
class TweetAnalysis:
    """Analysis results for a tweet."""
    tweet_id: str
    insight_score: float
    categories: list[str]
    summary: str
    blog_potential: str  # "high", "medium", "low"
    key_ideas: list[str]
    analyzed_at: datetime


class Database:
    """SQLite database for tweets and analysis."""

    def __init__(self, db_path: Path | str = "tweets.db"):
        self.db_path = Path(db_path)
        self._init_db()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tweets (
                    id TEXT PRIMARY KEY,
                    full_text TEXT NOT NULL,
                    created_at TEXT,
                    favorite_count INTEGER DEFAULT 0,
                    retweet_count INTEGER DEFAULT 0,
                    is_reply INTEGER DEFAULT 0,
                    is_retweet INTEGER DEFAULT 0,
                    is_quote INTEGER DEFAULT 0,
                    hashtags TEXT,
                    urls TEXT,
                    mentions TEXT,
                    raw_json TEXT
                );

                CREATE TABLE IF NOT EXISTS screening (
                    tweet_id TEXT PRIMARY KEY,
                    haiku_score REAL,
                    screened_at TEXT,
                    FOREIGN KEY (tweet_id) REFERENCES tweets(id)
                );

                CREATE TABLE IF NOT EXISTS analysis (
                    tweet_id TEXT PRIMARY KEY,
                    insight_score REAL,
                    categories TEXT,
                    summary TEXT,
                    blog_potential TEXT,
                    key_ideas TEXT,
                    analyzed_at TEXT,
                    FOREIGN KEY (tweet_id) REFERENCES tweets(id)
                );

                CREATE INDEX IF NOT EXISTS idx_insight_score ON analysis(insight_score DESC);
                CREATE INDEX IF NOT EXISTS idx_blog_potential ON analysis(blog_potential);
                CREATE INDEX IF NOT EXISTS idx_created_at ON tweets(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_haiku_score ON screening(haiku_score DESC);
            """)

    def insert_tweet(self, tweet: Tweet) -> bool:
        """Insert a tweet into the database. Returns True if inserted, False if exists."""
        with self._connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO tweets (id, full_text, created_at, favorite_count,
                        retweet_count, is_reply, is_retweet, is_quote, hashtags, urls, mentions, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tweet.id,
                        tweet.full_text,
                        tweet.created_at.isoformat(),
                        tweet.favorite_count,
                        tweet.retweet_count,
                        int(tweet.is_reply),
                        int(tweet.is_retweet),
                        int(tweet.is_quote),
                        json.dumps(tweet.hashtags),
                        json.dumps(tweet.urls),
                        json.dumps(tweet.mentions),
                        json.dumps(tweet.raw_json),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def insert_analysis(self, analysis: TweetAnalysis):
        """Insert or update analysis for a tweet."""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO analysis
                    (tweet_id, insight_score, categories, summary, blog_potential, key_ideas, analyzed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis.tweet_id,
                    analysis.insight_score,
                    json.dumps(analysis.categories),
                    analysis.summary,
                    analysis.blog_potential,
                    json.dumps(analysis.key_ideas),
                    analysis.analyzed_at.isoformat(),
                ),
            )

    def get_unanalyzed_tweets(self, min_length: int = 50, min_likes: int = 0, limit: int | None = None) -> list[dict]:
        """Get tweets that haven't been analyzed yet."""
        with self._connection() as conn:
            query = """
                SELECT t.* FROM tweets t
                LEFT JOIN analysis a ON t.id = a.tweet_id
                WHERE a.tweet_id IS NULL
                  AND LENGTH(t.full_text) >= ?
                  AND t.favorite_count >= ?
                  AND t.is_retweet = 0
                ORDER BY t.favorite_count DESC
            """
            if limit:
                query += f" LIMIT {limit}"

            rows = conn.execute(query, (min_length, min_likes)).fetchall()
            return [dict(row) for row in rows]

    def get_tweet_count(self) -> int:
        """Get total number of tweets in database."""
        with self._connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]

    def get_analyzed_count(self) -> int:
        """Get number of analyzed tweets."""
        with self._connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0]

    def get_screened_count(self) -> int:
        """Get number of screened tweets."""
        with self._connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM screening").fetchone()[0]

    def insert_screening(self, tweet_id: str, score: float):
        """Save Haiku screening result."""
        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO screening (tweet_id, haiku_score, screened_at) VALUES (?, ?, ?)",
                (tweet_id, score, datetime.now().isoformat()),
            )

    def get_unscreened_tweets(self, min_length: int = 30, min_likes: int = 0, limit: int | None = None) -> list[dict]:
        """Get tweets that haven't been screened by Haiku yet."""
        with self._connection() as conn:
            query = """
                SELECT t.* FROM tweets t
                LEFT JOIN screening s ON t.id = s.tweet_id
                LEFT JOIN analysis a ON t.id = a.tweet_id
                WHERE s.tweet_id IS NULL
                  AND a.tweet_id IS NULL
                  AND LENGTH(t.full_text) >= ?
                  AND t.favorite_count >= ?
                  AND t.is_retweet = 0
                ORDER BY t.favorite_count DESC
            """
            if limit:
                query += f" LIMIT {limit}"
            rows = conn.execute(query, (min_length, min_likes)).fetchall()
            return [dict(row) for row in rows]

    def get_screened_unanalyzed(self, threshold: float = 8.0, limit: int | None = None) -> list[dict]:
        """Get tweets that passed Haiku screening but haven't been analyzed by Sonnet."""
        with self._connection() as conn:
            query = """
                SELECT t.*, s.haiku_score FROM tweets t
                JOIN screening s ON t.id = s.tweet_id
                LEFT JOIN analysis a ON t.id = a.tweet_id
                WHERE a.tweet_id IS NULL
                  AND s.haiku_score >= ?
                ORDER BY s.haiku_score DESC
            """
            if limit:
                query += f" LIMIT {limit}"
            rows = conn.execute(query, (threshold,)).fetchall()
            return [dict(row) for row in rows]

    def get_top_tweets(
        self,
        limit: int = 20,
        min_score: float = 0,
        blog_potential: str | None = None,
        category: str | None = None,
    ) -> list[dict]:
        """Get top-scoring tweets with their analysis."""
        with self._connection() as conn:
            query = """
                SELECT t.*, a.insight_score, a.categories, a.summary,
                       a.blog_potential, a.key_ideas
                FROM tweets t
                JOIN analysis a ON t.id = a.tweet_id
                WHERE a.insight_score >= ?
            """
            params = [min_score]

            if blog_potential:
                query += " AND a.blog_potential = ?"
                params.append(blog_potential)

            if category:
                query += " AND a.categories LIKE ?"
                params.append(f'%"{category}"%')

            query += " ORDER BY a.insight_score DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["categories"] = json.loads(d["categories"]) if d["categories"] else []
                d["key_ideas"] = json.loads(d["key_ideas"]) if d["key_ideas"] else []
                results.append(d)
            return results

    def get_categories(self) -> list[tuple[str, int]]:
        """Get all categories and their tweet counts."""
        with self._connection() as conn:
            rows = conn.execute("SELECT categories FROM analysis WHERE categories IS NOT NULL").fetchall()

        category_counts: dict[str, int] = {}
        for row in rows:
            cats = json.loads(row["categories"]) if row["categories"] else []
            for cat in cats:
                category_counts[cat] = category_counts.get(cat, 0) + 1

        return sorted(category_counts.items(), key=lambda x: -x[1])

    def get_stats(self) -> dict:
        """Get database statistics."""
        with self._connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
            analyzed = conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0]
            high_potential = conn.execute(
                "SELECT COUNT(*) FROM analysis WHERE blog_potential = 'high'"
            ).fetchone()[0]
            avg_score = conn.execute(
                "SELECT AVG(insight_score) FROM analysis"
            ).fetchone()[0]

        return {
            "total_tweets": total,
            "analyzed_tweets": analyzed,
            "high_potential": high_potential,
            "avg_insight_score": round(avg_score, 2) if avg_score else 0,
        }

    def export_tweets(
        self,
        output_path: Path,
        format: str = "markdown",
        min_score: float = 7.0,
        blog_potential: str | None = "high",
    ):
        """Export high-scoring tweets to a file."""
        tweets = self.get_top_tweets(limit=100, min_score=min_score, blog_potential=blog_potential)

        if format == "markdown":
            self._export_markdown(tweets, output_path)
        elif format == "csv":
            self._export_csv(tweets, output_path)
        else:
            raise ValueError(f"Unknown format: {format}")

    def _export_markdown(self, tweets: list[dict], output_path: Path):
        """Export tweets to markdown format."""
        lines = ["# High-Potential Tweet Ideas for Blog Posts\n"]

        for i, tweet in enumerate(tweets, 1):
            score = tweet.get("insight_score", 0)
            categories = ", ".join(tweet.get("categories", []))
            ideas = tweet.get("key_ideas", [])

            lines.append(f"## {i}. Score: {score}/10 | {categories}\n")
            lines.append(f"> {tweet['full_text']}\n")
            lines.append(f"**Summary**: {tweet.get('summary', 'N/A')}\n")
            if ideas:
                lines.append("**Key Ideas**:")
                for idea in ideas:
                    lines.append(f"- {idea}")
            lines.append(f"\n*Likes: {tweet['favorite_count']} | RTs: {tweet['retweet_count']}*\n")
            lines.append("---\n")

        output_path.write_text("\n".join(lines))

    def _export_csv(self, tweets: list[dict], output_path: Path):
        """Export tweets to CSV format."""
        import csv

        with open(output_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "full_text", "insight_score", "blog_potential",
                "categories", "summary", "key_ideas", "likes", "retweets", "created_at"
            ])
            for tweet in tweets:
                writer.writerow([
                    tweet["id"],
                    tweet["full_text"],
                    tweet.get("insight_score", ""),
                    tweet.get("blog_potential", ""),
                    "|".join(tweet.get("categories", [])),
                    tweet.get("summary", ""),
                    "|".join(tweet.get("key_ideas", [])),
                    tweet["favorite_count"],
                    tweet["retweet_count"],
                    tweet["created_at"],
                ])
