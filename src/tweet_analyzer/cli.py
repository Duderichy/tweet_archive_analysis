"""Command-line interface for the Twitter Archive Analyzer."""

import sys
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from .parser import iter_tweets, get_archive_stats
from .database import Database
from .analyzer import TweetAnalyzer, AnalyzerConfig

console = Console()


def log(msg: str):
    """Print timestamped log message that works with file redirection."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


@click.group()
@click.option("--db", default="tweets.db", help="Path to SQLite database")
@click.pass_context
def cli(ctx, db):
    """Twitter Archive Analyzer - Find your most insightful tweets."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db


@cli.command("import")
@click.argument("archive_path", type=click.Path(exists=True))
@click.option("--include-rts", is_flag=True, help="Include retweets")
@click.pass_context
def import_archive(ctx, archive_path, include_rts):
    """Import tweets from a Twitter archive folder."""
    archive_path = Path(archive_path)
    db = Database(ctx.obj["db_path"])

    console.print(f"[bold]Importing from:[/bold] {archive_path}")

    # First, show archive stats
    with console.status("Scanning archive..."):
        stats = get_archive_stats(archive_path)

    console.print(f"  Total tweets: {stats['total']:,}")
    console.print(f"  Original: {stats['original']:,}")
    console.print(f"  Replies: {stats['replies']:,}")
    console.print(f"  Retweets: {stats['retweets']:,}")

    # Import tweets
    imported = 0
    skipped = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        total = stats["total"] - (0 if include_rts else stats["retweets"])
        task = progress.add_task("Importing...", total=total)

        for tweet in iter_tweets(archive_path, include_rts=include_rts):
            if db.insert_tweet(tweet):
                imported += 1
            else:
                skipped += 1
            progress.advance(task)

    console.print(f"\n[green]Imported:[/green] {imported:,} tweets")
    if skipped:
        console.print(f"[yellow]Skipped:[/yellow] {skipped:,} (already in database)")


@cli.command()
@click.option("--limit", "-n", type=int, help="Maximum tweets to screen")
@click.option("--min-length", default=30, help="Minimum tweet length")
@click.option("--min-likes", default=10, help="Minimum likes threshold")
@click.option("--threshold", default=8.0, help="Haiku screening threshold (0-10)")
@click.option("--dry-run", is_flag=True, help="Show count without analyzing")
@click.pass_context
def analyze(ctx, limit, min_length, min_likes, threshold, dry_run):
    """Analyze tweets using two-pass filtering (Haiku screen -> Sonnet detail)."""
    db = Database(ctx.obj["db_path"])
    config = AnalyzerConfig(screen_threshold=threshold, min_likes=min_likes)
    analyzer = TweetAnalyzer(db, config)

    # Show current status
    stats = db.get_stats()
    screened_count = db.get_screened_count()
    log(f"Database: {stats['total_tweets']:,} tweets, {screened_count:,} screened, {stats['analyzed_tweets']:,} analyzed")

    # Check what needs work
    est = analyzer.analyze_tweets(limit=limit, min_length=min_length, min_likes=min_likes, dry_run=True)

    if est['to_screen'] == 0 and est['cached_high'] == 0:
        log("All matching tweets have been analyzed!")
        return

    log(f"Cached (already screened, passed): {est['cached_high']:,}")
    log(f"To screen: {est['to_screen']:,} (with {min_likes}+ likes)")
    log(f"Phase 1: Haiku screens new tweets ({config.screen_batch_size}/batch)")
    log(f"Phase 2: Sonnet analyzes score >= {threshold} only")
    total = est['to_screen'] + est['cached_high']

    if dry_run:
        log(f"DRY RUN - Pre-filtered: ~{est.get('pre_filtered', 0):,}")
        log(f"DRY RUN - Cached high: {est['cached_high']:,} (skip Haiku)")
        log(f"DRY RUN - To screen: {est['to_screen']:,}")
        haiku_calls = (est['to_screen'] // config.screen_batch_size + 1) if est['to_screen'] > 0 else 0
        sonnet_input = est['cached_high'] + est['estimated_pass']
        sonnet_calls = (sonnet_input // config.detail_batch_size + 1) if sonnet_input > 0 else 0
        log(f"DRY RUN - Haiku calls: ~{haiku_calls}, Sonnet calls: ~{sonnet_calls}")
        return

    if not click.confirm("Continue with analysis?"):
        return

    log("Starting analysis...")
    last_log = {"screen": 0, "detail": 0}

    def update_progress(processed, phase_total, phase):
        # Log every 500 tweets or at completion
        if phase == "screening":
            if processed - last_log["screen"] >= 500 or processed == phase_total:
                log(f"Screening: {processed:,}/{phase_total:,} ({100*processed//phase_total}%)")
                last_log["screen"] = processed
        else:
            if processed - last_log["detail"] >= 100 or processed == phase_total:
                log(f"Analyzing: {processed:,}/{phase_total:,} ({100*processed//phase_total}%)")
                last_log["detail"] = processed

    result = analyzer.analyze_tweets(
        limit=limit,
        min_length=min_length,
        min_likes=min_likes,
        progress_callback=update_progress,
    )

    log(f"DONE - Pre-filtered: {result.get('pre_filtered', 0):,}")
    log(f"DONE - From cache: {result.get('cached', 0):,} (skipped Haiku)")
    log(f"DONE - Screened (Haiku): {result['screened']:,}")
    log(f"DONE - Passed to Sonnet: {result['passed_filter']:,}")
    log(f"DONE - Analyzed: {result['analyzed']:,}")

    # Update stats
    new_stats = db.get_stats()
    log(f"Total analyzed now: {new_stats['analyzed_tweets']:,} ({new_stats['high_potential']:,} high potential)")


@cli.command()
@click.option("--top", "-n", default=20, help="Number of tweets to show")
@click.option("--min-score", default=0.0, help="Minimum insight score")
@click.option("--potential", type=click.Choice(["high", "medium", "low"]), help="Filter by blog potential")
@click.option("--category", "-c", help="Filter by category")
@click.pass_context
def browse(ctx, top, min_score, potential, category):
    """Browse analyzed tweets."""
    db = Database(ctx.obj["db_path"])

    tweets = db.get_top_tweets(
        limit=top,
        min_score=min_score,
        blog_potential=potential,
        category=category,
    )

    if not tweets:
        console.print("[yellow]No tweets found matching criteria[/yellow]")
        return

    for i, tweet in enumerate(tweets, 1):
        score = tweet.get("insight_score", 0)
        categories = ", ".join(tweet.get("categories", []))
        potential_label = tweet.get("blog_potential", "?")

        # Color based on score
        if score >= 8:
            score_color = "green"
        elif score >= 6:
            score_color = "yellow"
        else:
            score_color = "white"

        # Build the panel
        title = f"[{score_color}]#{i} | Score: {score}/10[/{score_color}] | {potential_label.upper()}"

        content = Text()
        content.append(tweet["full_text"])
        content.append("\n\n")
        content.append(f"Categories: ", style="dim")
        content.append(categories)
        content.append("\n")
        content.append(f"Summary: ", style="dim")
        content.append(tweet.get("summary", "N/A"))

        ideas = tweet.get("key_ideas", [])
        if ideas:
            content.append("\n")
            content.append("Key Ideas: ", style="dim")
            content.append(" | ".join(ideas))

        content.append(f"\n\n[dim]Likes: {tweet['favorite_count']} | RTs: {tweet['retweet_count']}[/dim]")

        console.print(Panel(content, title=title, border_style="blue"))
        console.print()


@cli.command()
@click.pass_context
def stats(ctx):
    """Show database statistics."""
    db = Database(ctx.obj["db_path"])
    stats = db.get_stats()
    categories = db.get_categories()

    # Main stats table
    table = Table(title="Database Statistics", show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total Tweets", f"{stats['total_tweets']:,}")
    table.add_row("Analyzed", f"{stats['analyzed_tweets']:,}")
    table.add_row("High Potential", f"{stats['high_potential']:,}")
    table.add_row("Avg Insight Score", f"{stats['avg_insight_score']}")

    console.print(table)
    console.print()

    # Categories table
    if categories:
        cat_table = Table(title="Top Categories")
        cat_table.add_column("Category", style="cyan")
        cat_table.add_column("Count", style="green", justify="right")

        for cat, count in categories[:15]:
            cat_table.add_row(cat, str(count))

        console.print(cat_table)


@cli.command()
@click.argument("output", type=click.Path())
@click.option("--format", "-f", type=click.Choice(["markdown", "csv"]), default="markdown")
@click.option("--min-score", default=7.0, help="Minimum insight score")
@click.option("--potential", type=click.Choice(["high", "medium", "low"]), default="high")
@click.pass_context
def export(ctx, output, format, min_score, potential):
    """Export high-scoring tweets to a file."""
    db = Database(ctx.obj["db_path"])
    output_path = Path(output)

    db.export_tweets(
        output_path,
        format=format,
        min_score=min_score,
        blog_potential=potential,
    )

    console.print(f"[green]Exported to:[/green] {output_path}")


if __name__ == "__main__":
    cli()
