# Twitter Archive Analyzer

Find your most insightful tweets and identify blog post opportunities. Uses a two-pass Claude API pipeline: Haiku screens tweets quickly for insight potential, then Sonnet does detailed analysis on the top candidates.

## Setup

Using [uv](https://docs.astral.sh/uv/):

```bash
uv venv
uv pip install -e .
```

Or with standard Python:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Copy `.env.example` to `.env` and add your Anthropic API key:

```bash
cp .env.example .env
# Edit .env with your key
```

## Usage

### 1. Import your Twitter archive

Download your archive from Twitter/X (Settings > Your Account > Download an archive). Extract it, then:

```bash
tweet-analyzer import /path/to/twitter-archive
```

This parses the archive JS files and loads tweets into a local SQLite database (`tweets.db`).

Use `--include-rts` to include retweets.

### 2. Analyze tweets

```bash
tweet-analyzer analyze
```

This runs the two-pass pipeline:
- **Phase 1 (Haiku):** Screens tweets in batches, scoring each 0-10 for insight potential
- **Phase 2 (Sonnet):** Analyzes tweets that scored 8+ with detailed categorization, summaries, and blog potential ratings

Options:
- `--limit N` — cap the number of tweets to screen
- `--min-likes N` — only analyze tweets with N+ likes (default: 10)
- `--threshold N` — Haiku score cutoff for Phase 2 (default: 8.0)
- `--dry-run` — show counts without calling the API

Screening results are cached, so re-running only processes new tweets.

### 3. Browse results

```bash
tweet-analyzer browse
tweet-analyzer browse --min-score 8 --potential high
tweet-analyzer browse --category productivity -n 10
```

### 4. View stats

```bash
tweet-analyzer stats
```

### 5. Export

```bash
tweet-analyzer export output.md
tweet-analyzer export output.csv -f csv --min-score 6 --potential medium
```

## Global options

```bash
tweet-analyzer --db path/to/other.db <command>
```

All commands accept `--db` to use a different database file.
