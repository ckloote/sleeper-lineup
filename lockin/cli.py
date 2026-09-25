"""Command-line surface.

Read-only against Sleeper throughout: the API offers no way to act on your
behalf, so every recommendation is executed by hand in the app. `digest` and
`managers` are the two commands that write, and both write only to this
project's own tables — `recommendations`, `digest_runs`, `manager_scorecards`,
`manager_decisions`, `roster_strength`.

Three surfaces, three questions, and they are easy to confuse:

    digest      what do I do tonight          -> push notification
    advice      what did it say               -> a page you can re-read
    dashboard   who decided well last season  -> a page, retrospective
    serve       both pages, over HTTP         -> a phone on the LAN or tailnet

`advice` and `dashboard` are readers. Neither recomputes, and for `advice` that
is a correctness rule rather than a performance one — see lockin/advice.py.

The gates (`reconcile`, `verify`, `locks`, `calibrate`, `backtest`) live here
rather than in the test suite because they need the ingested season and take
seconds to minutes. Each exits nonzero on failure, so cron can treat them as
checks.

`observe` and `repair` are a pair, and both exist because Sleeper rewrites
completed seasons (implementation-plan.md §12). `observe` grows the snapshot
archive without opening the database; `repair` reads the archive back and
restores the values the database holds wrong. Neither is in the cron — the first
is, the second deliberately is not.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import click
import numpy as np

from lockin import advice as advice_mod
from lockin import backtest as backtest_mod
from lockin import calibrate as calibrate_mod
from lockin import clock
from lockin import dashboard as dashboard_mod
from lockin import digest as digest_mod
from lockin import locks as locks_mod
from lockin import managers as managers_mod
from lockin import notify as notify_mod
from lockin import projections as projections_mod
from lockin import reconcile as reconcile_mod
from lockin import repair as repair_mod
from lockin import serve as serve_mod
from lockin import shadow as shadow_mod
from lockin import verify as verify_mod
from lockin.config import ALL_STAT_WEEKS, Config, load_env_file
from lockin.core import projections as core_projections
from lockin.ingest import run as ingest_run
from lockin.ingest import sleeper as sleeper_ingest
from lockin.store import db, identity, snapshots
from lockin.store.db import session

CURRENT_WEEKS = "current"


def _parse_weeks(spec: str | None) -> list[int]:
    """Weeks named literally. `current` is resolved later; see `ingest`."""
    if not spec:
        return list(ALL_STAT_WEEKS)
    if spec.strip().lower() == CURRENT_WEEKS:
        raise ValueError(f"{CURRENT_WEEKS!r} is resolved from the league, not parsed here")
    out: list[int] = []
    try:
        for part in spec.split(","):
            if "-" in part:
                lo, hi = part.split("-", 1)
                out.extend(range(int(lo), int(hi) + 1))
            else:
                out.append(int(part))
    except ValueError:
        raise click.BadParameter(
            f"{spec!r} is not a week list: expected '12', '12,13', '1-25', or {CURRENT_WEEKS!r}"
        ) from None
    return out


def _parse_locked(spec: str | None) -> dict[str, float] | None:
    """'2126:42.5,1970:31' -> what is already banked.

    None and an empty string mean different things and must not be conflated:
    None asks the digest to reconstruct the state, while an explicit empty value
    asserts that nothing has been locked yet. Early in a week that assertion is
    both true and useful.
    """
    if spec is None:
        return None
    out: dict[str, float] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        sleeper_id, _, score = part.partition(":")
        if not score:
            raise click.BadParameter(f"expected 'sleeper_id:score', got {part!r}")
        try:
            out[sleeper_id.strip()] = float(score)
        except ValueError:
            raise click.BadParameter(f"{score!r} is not a score") from None
    return out


@click.group()
def main() -> None:
    """Sleeper NBA Lock-In lineup engine.

    Configuration comes from the environment, and from `.env` in the working
    directory for anything the environment does not already set — which is how
    the cron entries and the systemd unit, neither of which sources a shell
    profile, get `LOCKIN_DB`. See `lockin.config.load_env_file`.
    """
    try:
        load_env_file()
    except ValueError as exc:
        # Cron mails tracebacks. This one is a hand-edited config file, so say
        # which line and stop, rather than running against the default paths.
        raise click.ClickException(str(exc)) from None


def _missing_db(path: Path) -> click.ClickException:
    return click.ClickException(
        f"no database at {path}\n\n"
        "Nothing but `lockin ingest` creates one, deliberately: a database made on\n"
        "demand is an empty season, and every gate then reports `0/25 weeks\n"
        "ingested` — a missing ingest, when the truth is a missing setting.\n\n"
        "  wrong path   check LOCKIN_DB in .env, and that you are in the project\n"
        f"               directory (this is {Path.cwd()})\n"
        "  new host     copy last season across      (deployment.md step 3)\n"
        "  new season   `lockin ingest` creates it   (day-one.md step 2)"
    )


@contextmanager
def _season(cfg: Config) -> Iterator[sqlite3.Connection]:
    """`session`, but a database that is not there is an error, not an empty season.

    `ingest` is the one command that creates; see `lockin.store.db.connect`.
    Every other command is asking a question about a season, and there is no
    honest answer to give without one.

    It must also be the season configured: a `.env` still naming last season's
    file, or a `LOCKIN_SEASON` left behind at rollover, is refused here rather
    than answered from the wrong year (lockin/store/identity.py).
    """
    try:
        with session(cfg.db_path, create=False) as conn:
            try:
                identity.check(conn, cfg.league_id, cfg.season, db_path=cfg.db_path)
            except identity.IdentityMismatch as exc:
                raise click.ClickException(str(exc)) from None
            yield conn
    except db.DatabaseMissing as exc:
        raise _missing_db(exc.db_path) from None


@main.command()
@click.option(
    "--weeks",
    default=None,
    help="Weeks: '1-25', '12,13', or 'current' for the week Sleeper is playing. Default: all.",
)
@click.option("--skip-nba", is_flag=True, help="Skip the NBA schedule ingest.")
@click.option("--skip-tipoffs", is_flag=True, help="Skip the per-date tipoff sweep (slow).")
def ingest(weeks: str | None, skip_nba: bool, skip_tipoffs: bool) -> None:
    """Refresh league state, box scores, matchups and the NBA schedule.

    Also records today's injury designations, which Sleeper publishes only for
    today and never in history. That happens on every run and cannot be turned
    off: it is the one record here that cannot be backfilled, so it must not
    depend on remembering a flag. See `lockin.ingest.sleeper.ingest_players`.
    """
    cfg = Config.from_env()
    from_league = (weeks or "").strip().lower() == CURRENT_WEEKS
    week_list = None if from_league else _parse_weeks(weeks)

    # The one command that may bring a database into being: this is how a season
    # starts, on a fresh clone and on day one of the next one.
    with session(cfg.db_path, create=True) as conn:
        try:
            ingest_run.run_ingest(
                conn,
                cfg,
                client=sleeper_ingest.SleeperClient(),
                weeks=week_list,
                skip_nba=skip_nba,
                skip_tipoffs=skip_tipoffs,
                echo=click.echo,
            )
        except ingest_run.IngestRefused as exc:
            raise click.ClickException(str(exc)) from None

    click.echo("done. run `lockin reconcile` to check the Phase 0 gates.")


@main.command()
@click.option("--weeks", default=None, help="Weeks: '1-25' or '12,13'. Default: all.")
def observe(weeks: str | None) -> None:
    """Snapshot the matchup payloads. Watches upstream; writes no database.

    Sleeper keeps rewriting the completed 2025-26 season — which game counts for
    a player-week, not what the games were worth (implementation-plan.md §12).
    Three observations exist, all of them accidents of other work, spread over an
    interval nobody chose. This is the deliberate version.

    **It does not open the database, by design.** The mutation is upstream, and
    the point is to record it without re-ingesting: a full `lockin ingest`
    overwrites `box_scores`, moves `weekly_matchups` on, and would refetch a
    completed season into the file that preserves it. Snapshots dedup on
    content, so a stable week costs nothing and a moving one is written with the
    time it moved — which is what tells a scheduled batch job apart from cache
    eviction.

    Safe to run against any season, including one in progress; it only ever adds
    files under `snapshots/`.
    """
    cfg = Config.from_env()
    try:
        week_list = _parse_weeks(weeks)
    except ValueError as exc:
        raise click.BadParameter(
            f"{exc}. `observe` watches weeks that already happened, so name them."
        ) from None

    client = sleeper_ingest.SleeperClient()
    stamp = sleeper_ingest.snapshot_stamp()
    click.echo(f"league {cfg.league_id} season {cfg.season} -> {cfg.snapshot_root}")

    # Snapshot paths are keyed by season, so a league from another season would
    # file its payloads under this one's weeks. One call, before any file.
    league = sleeper_ingest.fetch_league(client, cfg.league_id)
    try:
        identity.check_payload(league, cfg.league_id, cfg.season)
    except identity.IdentityMismatch as exc:
        raise click.ClickException(str(exc)) from None
    finals = sleeper_ingest.mark_final_weeks(league, cfg.snapshot_root, cfg.season, stamp=stamp)
    if finals:
        click.echo(f"  final       week(s) {', '.join(map(str, finals))} now scored")

    changed = 0
    for week in week_list:
        before = snapshots.latest(cfg.snapshot_root, snapshots.MATCHUPS, cfg.season, week)
        payload = client.matchups(cfg.league_id, week)
        written = snapshots.save(
            cfg.snapshot_root,
            snapshots.MATCHUPS,
            cfg.season,
            week,
            payload,
            stamp=stamp,
        )
        if written is None:
            click.echo(f"  week {week:>2}     unchanged")
            continue

        changed += 1
        moved = snapshots.diff_counted(before, payload) if before is not None else []
        note = f"{len(moved)} starter values" if before is not None else "first observation"
        click.echo(f"  week {week:>2}     CHANGED  {note}  -> {written}")
        for roster_id, sleeper_id, was, now in moved[:3]:
            click.echo(f"               roster {roster_id} player {sleeper_id}  {was} -> {now}")
        if len(moved) > 3:
            click.echo(f"               ... and {len(moved) - 3} more")

    click.echo(f"  observed    {len(week_list)} weeks, {changed} changed")
    if changed:
        click.echo("commit the new snapshots — they cannot be refetched.")


@main.command()
@click.option("--weeks", default=None, help="Weeks: '1-25' or '12,13'. Default: all.")
@click.option("--apply", "do_apply", is_flag=True, help="Write the recovered values.")
@click.option("--stats", "show_stats", is_flag=True, help="Measure the archive; plan nothing.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.option(
    "--min-observations",
    default=repair_mod.MIN_OBSERVATIONS,
    show_default=True,
    help="Weeks with fewer snapshots than this are left alone.",
)
def repair(
    weeks: str | None, do_apply: bool, show_stats: bool, as_json: bool, min_observations: int
) -> None:
    """Restore the archive's consensus lock selections to the database.

    Sleeper rewrites completed seasons, but it does not lose them: a corrupted
    starter value reverts to the stored one at the next rewrite, and the value a
    slot keeps returning to agrees with the oldest snapshot 97% of the time
    (implementation-plan.md §12, "The locks are intact"). So with enough
    observations the stored value is usually recoverable by counting, and needs
    nothing from Sleeper. "Usually": a plurality or a tie is flagged as one.

    Only weeks Sleeper has finished scoring, and only snapshots taken after
    that: during a live week an early zero is a reading, not corruption.

    Reports by default and writes only under `--apply`. The write is an append:
    corrupted rows stay in `weekly_matchups` as history, readers go through
    `weekly_matchups_latest`, and one `observed_at` is the whole undo.

    `--stats` skips the database entirely and measures the archive against
    itself — the reversion rates, the excursion lengths, and how many completed
    matchups a given read gets backwards.
    """
    cfg = Config.from_env()
    try:
        week_list = _parse_weeks(weeks)
    except ValueError as exc:
        raise click.BadParameter(f"{exc}. `repair` works on weeks already observed.") from None

    if show_stats:
        st = repair_mod.stats(
            cfg.snapshot_root, cfg.season, week_list, min_observations=min_observations
        )
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "weeks": st.weeks,
                        "slots": st.slots,
                        "observations": st.observations,
                        "earliest_agrees": [st.earliest_agrees, st.earliest_total],
                        "tied_slots": st.tied_slots,
                        "stays_on_majority": [st.stay_on, st.stay_on_total],
                        "returns_to_majority": [st.return_to, st.return_to_total],
                        "excursion_lengths": dict(sorted(st.excursions.items())),
                        "currently_off": st.currently_off,
                        "wrong_winners": [st.matchups_wrong, st.matchups_total],
                    },
                    indent=2,
                )
            )
        else:
            for line in st.lines:
                click.echo(line)
        return

    with _season(cfg) as conn:
        repairs, skipped = repair_mod.plan(
            conn,
            cfg.snapshot_root,
            cfg.season,
            week_list,
            min_observations=min_observations,
        )
        still_open = repair_mod.open_weeks(cfg.snapshot_root, cfg.season, week_list)

        if as_json:
            click.echo(
                json.dumps(
                    {
                        "applied": do_apply,
                        "skipped_weeks": skipped,
                        "open_weeks": still_open,
                        "consensus": [
                            {
                                "week": r.consensus.week,
                                "roster_id": r.consensus.roster_id,
                                "sleeper_id": r.consensus.sleeper_id,
                                "database": r.db_value,
                                "archive": r.consensus.value,
                                "votes": r.consensus.votes,
                                "observations": r.consensus.observations,
                                "tied": r.consensus.tied,
                                "strength": r.consensus.strength,
                            }
                            for r in repairs
                        ],
                    },
                    indent=2,
                )
            )
        else:
            click.echo(f"league {cfg.league_id} season {cfg.season} <- {cfg.snapshot_root}")
            if still_open:
                click.echo(
                    f"  open        weeks {still_open} — not scored yet; live polls are"
                    " readings, not evidence"
                )
            if skipped:
                click.echo(
                    f"  skipped     weeks {skipped} — fewer than {min_observations} final"
                    " observations"
                )
            by_week: dict[int, list[repair_mod.Repair]] = {}
            for r in repairs:
                by_week.setdefault(r.consensus.week, []).append(r)
            for week, items in sorted(by_week.items()):
                click.echo(f"  week {week:>2}     {len(items)} starter values")
                for r in items[:3]:
                    c = r.consensus
                    flag = "" if c.strength == "majority" else f"  {c.strength.upper()}"
                    click.echo(
                        f"               roster {c.roster_id} player {c.sleeper_id}"
                        f"  {r.db_value} -> {c.value}"
                        f"  ({c.votes}/{c.observations}){flag}"
                    )
                if len(items) > 3:
                    click.echo(f"               ... and {len(items) - 3} more")
            click.echo(f"  planned     {len(repairs)} values across {len(by_week)} weeks")

        if not repairs:
            return
        if not do_apply:
            if not as_json:
                click.echo("nothing written. re-run with --apply to restore these values.")
            return

        rows, teams = repair_mod.apply(
            conn,
            cfg.snapshot_root,
            cfg.season,
            repairs,
            observed_at=db.now_iso(),
            min_observations=min_observations,
        )
        db.checkpoint(conn)
        if not as_json:
            click.echo(f"  applied     {rows} starter rows, {teams} team totals")
            click.echo("re-run `lockin managers` and `lockin dashboard` — their inputs moved.")


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def reconcile(as_json: bool) -> None:
    """Report on ingest completeness. Exits nonzero if a gate fails."""
    cfg = Config.from_env()
    with _season(cfg) as conn:
        checks = reconcile_mod.run(conn, cfg.season, cfg.snapshot_root)

    _render(checks, "Phase 0 reconciliation", as_json)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def verify(as_json: bool) -> None:
    """Prove the scoring engine against the recorded season. Nonzero on failure."""
    cfg = Config.from_env()
    with _season(cfg) as conn:
        checks = verify_mod.run(conn, cfg.season)

    _render(checks, "Phase 1 scoring verification", as_json)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.option("--profiles", is_flag=True, help="Print each manager's lock tendency.")
def locks(as_json: bool, profiles: bool) -> None:
    """Recover every manager's lock decisions from the recorded season."""
    cfg = Config.from_env()
    with _season(cfg) as conn:
        rows, resolved = locks_mod.run_inference(conn, cfg.season)
        built = locks_mod.build_profiles(conn)
        checks = locks_mod.run(conn, cfg.season)
        breakdown = locks_mod.status_breakdown(conn)

    if not as_json:
        click.echo(f"inferred {rows} starter player-weeks, {resolved} resolved\n")
        for status, n in breakdown:
            click.echo(f"  {status:<20} {n:>5}")
        click.echo()
        if profiles:
            click.echo("manager lock tendency (higher lock_rate = banks earlier)")
            click.echo(
                f"  {'roster':>6}  {'decisions':>9}  {'early':>5}  {'rode':>5}"
                f"  {'lock_rate':>9}  {'mean_pos':>8}"
            )
            for p in sorted(built, key=lambda x: -x.lock_rate):
                pos = f"{p.mean_lock_position:.2f}" if p.mean_lock_position is not None else "  -"
                click.echo(
                    f"  {p.roster_id:>6}  {p.decisions:>9}  {p.locked_early:>5}"
                    f"  {p.rode_to_end:>5}  {p.lock_rate:>9.1%}  {pos:>8}"
                )
            click.echo()

    _render(checks, "Phase 2 lock inference", as_json)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.option("--draws", default=1000, show_default=True, help="Monte Carlo draws per player-game.")
@click.option(
    "--holdout-from",
    default=calibrate_mod.DEFAULT_HOLDOUT_FROM,
    show_default=True,
    help="First held-out fantasy week. Earlier weeks tuned the model.",
)
@click.option(
    "--cold-start",
    is_flag=True,
    help="Check the first month, with no burn-in, against the digest's abstention threshold.",
)
def calibrate(as_json: bool, draws: int, holdout_from: int, cold_start: bool) -> None:
    """Check the projection layer's quantiles against what happened. Nonzero on failure."""
    cfg = Config.from_env()
    if cold_start:
        with _season(cfg) as conn:
            sample, pool = calibrate_mod.evaluate_cold_start(
                conn, cfg.season, n_draws=min(draws, 500)
            )
        if not as_json:
            click.echo(f"projected {len(sample)} player-games in weeks 1-4, no burn-in\n")
            click.echo("  pool rows before     n    P(> q0.90)   P(> q0.99)")
            edges = [0, 100, 200, 300, 400, 500, 700, 1000, 1500, 2500, 10**9]
            for lo, hi in zip(edges, edges[1:], strict=False):
                mask = (pool >= lo) & (pool < hi)
                if mask.sum() < 30:
                    continue
                band = sample._select(mask)
                a, b = calibrate_mod._z(band, 0.90), calibrate_mod._z(band, 0.99)
                label = f"{lo}-{hi}" if hi < 10**9 else f"{lo}+"
                click.echo(
                    f"  {label:<16} {mask.sum():>5}   {a[0]:.3f} ({a[1]:+.1f})"
                    f"   {b[0]:.4f} ({b[1]:+.1f})"
                )
            click.echo()
        _render(calibrate_mod.cold_start_checks(sample, pool), "Cold-start calibration", as_json)
        return
    with _season(cfg) as conn:
        checks, sample = calibrate_mod.run(
            conn, cfg.season, n_draws=draws, holdout_from=holdout_from
        )
        held = sample.holdout(holdout_from)

    if not as_json:
        click.echo(
            f"projected {len(sample)} player-games; {len(held)} held out"
            f" (weeks {holdout_from}-25)\n"
        )
        click.echo("  PIT deciles, held out (each should be 0.100)")
        click.echo("    " + " ".join(f"{x:.3f}" for x in calibrate_mod.pit_histogram(held)))
        click.echo()

    _render(checks, "Phase 3 projection calibration", as_json)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.option("--paths", default=400, show_default=True, help="Simulated paths per decision.")
@click.option(
    "--holdout-from",
    default=backtest_mod.DEFAULT_HOLDOUT_FROM,
    show_default=True,
    help="First held-out fantasy week.",
)
def backtest(as_json: bool, paths: int, holdout_from: int) -> None:
    """Replay every roster under each stopping policy. Nonzero on failure."""
    cfg = Config.from_env()
    with _season(cfg) as conn:
        checks, result = backtest_mod.run(
            conn, cfg.season, n_paths=paths, holdout_from=holdout_from
        )
        held = result.holdout(holdout_from)

    if not as_json:
        click.echo(
            f"replayed {len(result.rows)} roster-weeks;"
            f" {len(held.rows)} held out (weeks {holdout_from}-25),"
            f" {held.starters()} starter-weeks\n"
        )
        # Every mean is taken over the roster-weeks where ROLLOUT also ran, so
        # the column is comparable down its length. Rollout needs an opponent
        # and so is absent from weeks 23-24's eliminated teams and from unscored
        # week 25; averaging each policy over its own rows would compare
        # different sets of weeks and flatter whichever set was easier.
        comparable = [r for r in held.rows if backtest_mod.ROLLOUT in r.points]
        n_common = len(comparable)
        click.echo(
            f"  means over the {n_common} of {len(held.rows)} held-out roster-weeks"
            f" where every policy ran"
        )
        click.echo(f"  {'policy':<12} {'points':>8} {'zeroed':>8} {'locked':>8} {'wins':>9}")
        for name in (*backtest_mod.REPLAYED_POLICIES, backtest_mod.ORACLE):
            pts = float(np.mean([r.points[name] for r in comparable])) if comparable else 0.0
            zeroed = sum(r.zeroed.get(name, 0) for r in comparable)
            if name == backtest_mod.ORACLE:
                click.echo(
                    f"  {name:<12} {pts:>8.1f} {zeroed:>8} {'-':>8} {'-':>9}"
                    "   perfect foresight, not attainable"
                )
                continue
            locked = sum(r.locked.get(name, 0) for r in comparable)
            won, played = backtest_mod.wins_flipped(held, name)
            click.echo(f"  {name:<12} {pts:>8.1f} {zeroed:>8} {locked:>8} {f'{won}/{played}':>9}")
        actual = [r.actual_points for r in held.rows if r.actual_points is not None]
        if actual:
            click.echo(
                f"  {'actual':<12} {sum(actual) / len(actual):>8.1f} {'-':>8} {'-':>8} {'-':>9}"
                "   advisory: reads the field Sleeper rewrote"
            )
        click.echo("\n  wins are head-to-head with the opponent left on never-lock.")

        pairs = backtest_mod.head_to_head(result, backtest_mod.ROLLOUT, backtest_mod.GREEDY)
        b, c, z = backtest_mod.mcnemar(pairs)
        click.echo(
            f"\n  rollout vs greedy, both against a greedy opponent, all ten rosters:"
            f"\n    {len(pairs)} team-weeks — rollout {int(pairs[:, 0].sum())} wins,"
            f" greedy {int(pairs[:, 1].sum())}; flipped +{b}/-{c}, McNemar z={z:+.2f}\n"
        )

    _render(checks, "Phase 4-5 stopping-policy backtest", as_json)


def _manager_labels(cfg: Config, conn, *, refresh: bool) -> dict[int, str]:
    """Display names for the roster columns, read from `league_users`.

    `lockin ingest` stores them, so the normal case is a table read and
    `--names` only forces a fresh fetch first. One writer, one reader: before
    this, every caller fetched from the API itself, and `lockin serve` — which
    cannot, holding a read-only connection — was left labelling rows "roster 3".
    """
    if refresh:
        sleeper_ingest.ingest_users(conn, sleeper_ingest.SleeperClient(), cfg.league_id)
    return dashboard_mod.labels(conn)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.option(
    "--names",
    is_flag=True,
    help="Refresh display names from Sleeper first. Default: what `lockin ingest` stored.",
)
def teams(as_json: bool, names: bool) -> None:
    """Rank teams on roster quality — how good the side was, not how it was run."""
    cfg = Config.from_env()
    with _season(cfg) as conn:
        strengths = managers_mod.evaluate_rosters(conn, cfg.season)
        managers_mod.persist_rosters(conn, strengths)
        labels = _manager_labels(cfg, conn, refresh=names)

    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "rank": i,
                        "roster_id": r.roster_id,
                        "manager": labels.get(r.roster_id),
                        "ceiling": r.ceiling,
                        "realised_ceiling": r.realised_ceiling,
                        "lineup_gap": r.lineup_gap,
                        "talent_per_game": r.talent_per_game,
                        "availability": r.availability,
                        "points_per_game_played": r.points_per_game_played,
                        "games_per_week": r.games_per_week,
                    }
                    for i, r in enumerate(strengths, 1)
                ],
                indent=2,
            )
        )
        return

    click.echo(
        "Teams on paper. Ranked by ceiling: the best legal six from the WHOLE roster,\n"
        "every lock perfect — so lineup selection and stopping skill are both removed.\n"
    )
    click.echo(
        f"  {'#':>2} {'roster':>6} {'manager':<16} {'ceiling':>8} {'available':>10}"
        f" {'pts/game':>9} {'talent/gm':>10} {'lineup cost':>12}"
    )
    for i, r in enumerate(strengths, 1):
        click.echo(
            f"  {i:>2} {r.roster_id:>6} {labels.get(r.roster_id, ''):<16} {r.ceiling:>8.1f}"
            f" {r.availability:>9.1%} {r.points_per_game_played:>9.1f}"
            f" {r.talent_per_game:>10.1f} {r.lineup_gap:>12.1f}"
        )
    click.echo(
        "\n  ceiling is produced by two things, both shown: how often the roster was"
        "\n  available, and how much it scored when it was. Durability is already inside"
        "\n  ceiling — a missed week counts zero — so a star who scores 100 a night and"
        "\n  misses the season is correctly worth nothing here."
        "\n\n  Neither column needs injury data: `played` says who suited up. Injury data"
        "\n  would say WHY, and whether it was known in advance — which is what start/sit"
        "\n  evaluation needs and team quality does not (§17)."
        "\n\n  What this cannot do: separate durability skill from health luck, or predict"
        "\n  next season. It is a record of what happened, not a forecast."
        "\n  'lineup cost' is a decision, not roster quality — see `lockin managers`."
    )


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.option("--sims", default=300, show_default=True, help="Simulations per decision.")
@click.option(
    "--names",
    is_flag=True,
    help="Refresh display names from Sleeper first. Default: what `lockin ingest` stored.",
)
@click.option(
    "--competitive",
    is_flag=True,
    help="Only decisions with the matchup live (P(win) 30-70%), matching everyone on difficulty.",
)
def managers(as_json: bool, sims: int, names: bool, competitive: bool) -> None:
    """Rank the managers on decision quality, holding roster talent constant."""
    cfg = Config.from_env()
    with _season(cfg) as conn:
        report = managers_mod.evaluate_managers(
            conn, cfg.season, n_sims=sims, competitive_only=competitive
        )
        n_decisions, n_cards = managers_mod.persist(conn, report)
        strengths = managers_mod.evaluate_rosters(conn, cfg.season)
        managers_mod.persist_rosters(conn, strengths)
        labels = _manager_labels(cfg, conn, refresh=names)

    ranked = report.ranked()
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "rank": i,
                        "roster_id": s.roster_id,
                        "manager": labels.get(s.roster_id),
                        "squandered_share": s.squandered_share,
                        "mean_stake": s.mean_stake,
                        "mean_regret": s.mean_regret,
                        "right_rate": s.right_rate,
                        "decisions": s.decisions,
                        "divergent": s.divergent,
                        "divergent_right_rate": s.divergent_right_rate,
                        "upside_share": s.upside_share,
                        "rode_to_zero": s.rode_to_zero,
                    }
                    for i, s in enumerate(ranked, 1)
                ],
                indent=2,
            )
        )
        return

    agree, differ = report.regret_by_agreement()
    scope = "competitive decisions only" if competitive else "all decisions"
    click.echo(
        f"{len(report.decisions)} decisions across {len(ranked)} managers ({scope})."
        f"\nRanked by the share of at-stake win probability thrown away — lower is better.\n"
    )
    click.echo(
        f"  {'#':>2} {'roster':>6} {'manager':<16} {'squander':>9} {'wrong':>7} {'stake':>7}"
        f" {'regret':>8} {'n':>5} {'hi-lev':>7} {'pts cap':>8} {'zeros':>6}"
    )
    for i, s in enumerate(ranked, 1):
        click.echo(
            f"  {i:>2} {s.roster_id:>6} {labels.get(s.roster_id, ''):<16}"
            f" {s.squandered_share:>8.1%} {1 - s.right_rate:>6.1%} {s.mean_stake:>6.2%}"
            f" {s.mean_regret:>7.3%} {s.decisions:>5}"
            f" {s.divergent_right_rate:>6.0%} {s.upside_share:>7.1%} {s.rode_to_zero:>6}"
        )
    click.echo(
        f"\n  'squander' is regret as a share of what was at stake, which divides out"
        f" circumstance:\n  raw regret is P(wrong) x E[stake], and a hopeless matchup carries"
        f" a mean stake of 3.0%\n  against 10.4% in a live one. 'stake' is that circumstance,"
        f" shown so it can be judged.\n  Run with --competitive to match everyone on difficulty"
        f" (Spearman +0.94 with this)."
        f"\n\n  points and win probability disagree on {report.divergence_rate():.1%}"
        f" of decisions; mean regret {differ:.3%} there against {agree:.3%} elsewhere."
        f"\n  'hi-lev' is the right-side rate on just those decisions."
        f"\n  'pts cap' is the older points-capture metric, shown for contrast only —"
        f" it scores correct\n  variance-taking as a blunder, which is why it is not the ranking."
        f"\n\n  Reads the field Sleeper rewrote (§12): this is how the current data makes"
        f"\n  each manager look, not a certified record. Do not benchmark the engine on"
        f"\n  this scale — it is graded by its own model."
        f"\n\n  wrote {n_decisions} rows to manager_decisions and {n_cards} to"
        f" manager_scorecards,\n  which is what a dashboard should read."
    )

    click.echo(
        "\n\nTeams on paper — how good the roster was, as distinct from how it was run."
        "\nRanked by ceiling: the best legal six from the WHOLE roster, every lock perfect.\n"
    )
    click.echo(
        f"  {'#':>2} {'roster':>6} {'manager':<16} {'ceiling':>8} {'oracle':>8}"
        f" {'lineup cost':>12} {'talent/gm':>10} {'games/wk':>9}"
    )
    for i, r in enumerate(strengths, 1):
        click.echo(
            f"  {i:>2} {r.roster_id:>6} {labels.get(r.roster_id, ''):<16}"
            f" {r.ceiling:>8.1f} {r.realised_ceiling:>8.1f} {r.lineup_gap:>12.1f}"
            f" {r.talent_per_game:>10.1f} {r.games_per_week:>9.2f}"
        )
    click.echo(
        "\n  'lineup cost' is what the lineups cost, NOT whether they were mistakes."
        " Judging that\n  needs an injury feed: the model's own lineup picks are ~20"
        " pts/week WORSE than the\n  managers' (§16), because it cannot see who is out."
        " The decision ranking above is\n  lock/pass only.\n"
        "\n  'oracle' is the same ceiling over only the six actually started, so"
        " 'lineup cost'\n  is the price of starting the wrong players — a decision, not"
        " roster quality.\n  'talent/gm' values the same lineup per game rather than per"
        " week; schedule density\n  spans only 3.25-3.40 games a week here, so it changes"
        " little.\n\n  Built from what players actually did, so health and form are in it."
        " A truly ex-ante\n  measure would have to come from the projection layer."
    )


@main.command()
@click.option("--date", "as_of", default=None, help="As-of date, YYYY-MM-DD. Default: today.")
@click.option("--roster", type=int, default=None, help="Roster id. Default: yours.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.option("--sims", default=400, show_default=True, help="Simulated paths per decision.")
@click.option("--notify", is_flag=True, help="Send the digest as a push notification.")
@click.option("--no-write", is_flag=True, help="Do not record the advice in `recommendations`.")
@click.option(
    "--locked",
    default=None,
    help="What you have already banked, as 'sleeper_id:score,...'. Default: reconstructed.",
)
def digest(
    as_of: str | None,
    roster: int | None,
    as_json: bool,
    sims: int,
    notify: bool,
    no_write: bool,
    locked: str | None,
) -> None:
    """The daily recommendation: what to lock now, and the standing rules.

    Reconstructs the *morning* of the given date. Nothing on or after it is
    read — see lockin/digest.py — so this is the same code path that will run
    live, exercised against the only season that exists.

    Pass --locked whenever you know what you have banked. Reconstructing it is a
    chain of near-tied calls and it is the noisiest thing here; supplying it
    removes that noise instead of averaging over it.
    """
    cfg = Config.from_env()
    today = clock.today_iso(cfg.timezone)
    as_of = as_of or today
    # A run for today is live: it reads today's rosters, refuses an unfinished
    # slate, and closes calls whose tip has passed. A past date is a replay.
    live = as_of == today
    now = datetime.now(UTC) if live else None
    banked = _parse_locked(locked)

    with _season(cfg) as conn:
        roster_id = roster or digest_mod.roster_for_user(conn, cfg.user_id)
        if roster_id is None:
            raise click.ClickException(
                f"no roster for user {cfg.user_id}; run `lockin ingest` or pass --roster"
            )
        try:
            report = digest_mod.morning(
                conn,
                cfg.season,
                roster_id,
                as_of,
                n_sims=sims,
                locked=banked,
                live=live,
                now=now,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        written = (
            0 if no_write else digest_mod.persist(conn, report, state_supplied=banked is not None)
        )

    if as_json:
        click.echo(
            json.dumps(
                {
                    "as_of": report.as_of,
                    "week": report.week,
                    "roster_id": report.roster_id,
                    "opponent_roster_id": report.opponent_roster_id,
                    "note": report.note,
                    "p_win": report.p_win,
                    "projected": report.my_total,
                    "opponent_projected": report.opponent_total,
                    "margin": report.margin,
                    "banked": {report.names.get(k, k): v for k, v in report.banked.items()},
                    "state_source": report.state_source,
                    "opponent_state": report.opponent_state,
                    "calls": [
                        {
                            "player": c.name,
                            "sleeper_id": c.sleeper_id,
                            "date": projections_mod.date_of(c.day),
                            "score": c.score,
                            "action": "LOCK" if c.lock else "PASS",
                            "break_even": c.break_even if np.isfinite(c.break_even) else None,
                            "p_win_lock": c.p_win_lock,
                            "p_win_pass": c.p_win_pass,
                            "expires_utc": c.expires_utc,
                        }
                        for c in report.calls
                    ],
                    "standing_rules": [
                        {
                            "player": r.name,
                            "sleeper_id": r.sleeper_id,
                            "night": projections_mod.date_of(r.night),
                            "threshold": r.threshold,
                            "p_clear": None if np.isnan(r.p_clear) else r.p_clear,
                            "idle_nights_assumed": r.idle_nights,
                            "games_after": r.games_after,
                        }
                        for r in report.rules
                    ],
                    "warnings": [
                        {"player": w.name, "kind": w.kind, "detail": w.detail}
                        for w in report.warnings
                    ],
                    "recommendations_written": written,
                },
                indent=2,
            )
        )
        return

    click.echo(digest_mod.render(report))
    if report.note is None:
        source = {
            "supplied": "banked state as given on the command line",
            "inferred": f"banked state read from the {report.poll_observed_at} poll;\n"
            "  last night's games are the calls above, whatever you did with them",
            "assumed": "banked state ASSUMES you followed this engine on every\n"
            "  closed window — a replay with no poll from that morning;\n"
            "  pass --locked when you know it (§20)",
        }.get(report.state_source, "banked state unknown")
        if report.opponent_state == "stand-in":
            source += ".\n  Opponent's locks: the greedy base policy stands in (no poll)"
        click.echo(
            f"\n  {source}."
            f"\n  Thresholds carry 1-3 points of Monte Carlo noise at --sims {sims};"
            f"\n  the lock/pass calls above are stable from 400."
            f"\n  {written} row(s) written to recommendations."
        )
    if notify:
        sent = notify_mod.send(digest_mod.render(report, compact=True), cfg)
        click.echo(f"  notification: {sent}")


@main.command()
@click.argument("player")
@click.option(
    "--as-of", "as_of", required=True, help="Game date, YYYY-MM-DD. History before it only."
)
@click.option("--week", type=int, required=True, help="Fantasy week of the game being projected.")
@click.option("--draws", default=4000, show_default=True, help="Monte Carlo draws.")
def project(player: str, as_of: str, week: int, draws: int) -> None:
    """Print one player's projected score distribution for a single game."""
    import numpy as np

    cfg = Config.from_env()
    with _season(cfg) as conn:
        panel = projections_mod.load_panel(conn, cfg.season)
        source = core_projections.EWMAProjectionSource(panel, verify_mod.scoring_settings(conn))
        dist = source.project(
            player,
            projections_mod.day_index(as_of),
            fantasy_week=week,
            rng=np.random.default_rng(0),
            n_draws=draws,
        )

    click.echo(f"player {player}  {as_of}  week {week}")
    click.echo(f"  basis        {dist.basis} ({dist.n_own_games} prior played games)")
    click.echo(f"  P(does not play) {dist.p_dnp:.1%}")
    click.echo(f"  mean         {dist.mean:.1f}")
    for q in (0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        click.echo(f"  q{q:<11.2f} {float(dist.quantile(q)):.1f}")


@main.command()
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("advice.html"),
    show_default=True,
    help="Where to write the page.",
)
@click.option("--roster", type=int, default=None, help="Roster id. Default: yours.")
def advice(out: Path, roster: int | None) -> None:
    """Render the last digest as a page: what to lock, and the standing rules.

    Reads `recommendations` and `digest_runs`. It never recomputes, which here is
    a correctness rule rather than a performance one — recomputing gives a
    different answer (§20) and the upstream inputs are rewritten under us (§12),
    so this is the only way to see what the engine actually said.
    """
    cfg = Config.from_env()
    with _season(cfg) as conn:
        roster_id = roster or digest_mod.roster_for_user(conn, cfg.user_id)
        if roster_id is None:
            raise click.ClickException(f"no roster for user {cfg.user_id}")
        run = advice_mod.latest_run(conn, roster_id)

    if run is None:
        raise click.ClickException(
            f"no digest recorded for roster {roster_id}; run `lockin digest` first"
        )

    out.write_text(advice_mod.render(run))
    age = run.age_days()
    freshness = "for this morning" if age <= 0 else f"{age} day(s) old"
    click.echo(f"wrote {out} — week {run.week}, {freshness} ({run.as_of})")
    click.echo(
        f"  {len(run.calls)} call(s), {len(run.rules)} standing rule(s),"
        f" generated {run.generated_at}"
    )
    if age > 0:
        click.echo("  the page says so in a red banner; re-run `lockin digest` to refresh.")


@main.command()
def shadow() -> None:
    """Compare what the digest said with what was then done, week by week.

    For each week the league has finished scoring: whether each call was
    followed, whether each morning's BANKED list matched the locks the final
    scores reveal, P(win) against results, and calls that changed between runs.
    Ends with the day-one.md step 7 gate. Writes nothing.
    """
    cfg = Config.from_env()
    with _season(cfg) as conn:
        report = shadow_mod.build(conn, cfg.season)
    click.echo(shadow_mod.render(report))


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True, help="Interface to bind.")
@click.option("--port", default=serve_mod.PORT, show_default=True, help="Port to bind.")
@click.option("--roster", type=int, default=None, help="Roster id. Default: yours.")
@click.option(
    "--dashboard-db",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Database for /dashboard. Default: the same one. In-season, point this at "
    "last season, whose scorecards are the only ones that exist.",
)
@click.option("--quiet", is_flag=True, help="Do not log requests.")
def serve(host: str, port: int, roster: int | None, dashboard_db: Path | None, quiet: bool) -> None:
    """Serve the advice and dashboard pages over HTTP.

    Rendered from the database on each request, so the page cannot be older than
    the last digest. Read-only, and it never opens a file — `python -m
    http.server` pointed here would publish `data/lockin.db` and `snapshots/`.

    Binds all interfaces by default, which is what makes it reachable from a
    phone and from Tailscale. There is no authentication: the network is the
    boundary, so do not port-forward it.
    """
    cfg = Config.from_env()
    # Checked here rather than on the first request: this runs under systemd, and
    # a typo in `--dashboard-db` should fail the unit at start, not surface as a
    # 500 the next time someone opens /dashboard on their phone.
    if dashboard_db is not None and not dashboard_db.exists():
        raise _missing_db(dashboard_db)
    with _season(cfg) as conn:
        roster_id = roster or digest_mod.roster_for_user(conn, cfg.user_id)
    if roster_id is None:
        raise click.ClickException(f"no roster for user {cfg.user_id}; pass --roster")

    httpd = serve_mod.build_server(cfg.db_path, roster_id, host=host, port=port, quiet=quiet)
    click.echo(f"serving roster {roster_id} from {cfg.db_path} (read-only)")
    for url in serve_mod.reachable_addresses(port):
        click.echo(f"  {url}")
    click.echo("  /            what to do tonight")
    click.echo("  /dashboard   who decided well last season")
    if dashboard_db is None:
        click.echo(
            "  (--dashboard-db is unset: in-season /dashboard will be empty until"
            "\n   `lockin managers` runs, since scorecards are retrospective)"
        )
    if host == "0.0.0.0":  # noqa: S104 - the default, and deliberately so
        click.echo(
            "\n  bound to all interfaces: anyone who can route here can read your"
            "\n  lineup. Fine on a LAN or a tailnet; do not port-forward it."
            "\n  --host 127.0.0.1 restricts it to this machine."
        )
    click.echo("\nCtrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nstopped.")
    finally:
        httpd.server_close()


@main.command()
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("dashboard.html"),
    show_default=True,
    help="Where to write the page.",
)
@click.option(
    "--names",
    is_flag=True,
    help="Refresh display names from Sleeper first. Default: what `lockin ingest` stored.",
)
def dashboard(out: Path, names: bool) -> None:
    """Render the manager-quality page from what `lockin managers` stored.

    A second reader of SQLite, never a second computation: the Monte Carlo behind
    these rows costs seconds, which is fine for a command and hopeless for a page
    load.
    """
    cfg = Config.from_env()
    with _season(cfg) as conn:
        labels = _manager_labels(cfg, conn, refresh=names)
        rows = dashboard_mod.load(conn, labels)
        stamp = dashboard_mod.computed_at(conn)

    if not rows:
        raise click.ClickException("no scorecards stored; run `lockin managers` first")

    out.write_text(dashboard_mod.render(rows, stamp=stamp))
    click.echo(f"wrote {out} — {len(rows)} managers, ranked on squandered_share")
    click.echo(
        "  sorted on the stake-normalised share, never on points capture;"
        "\n  the 90% bands overlap across most of the table and are drawn so"
        "\n  that is visible. Open it in a browser, or serve the file."
    )


@main.command()
@click.argument("player")
@click.option("--date", "as_of", default=None, help="As-of date, YYYY-MM-DD. Default: today.")
@click.option("--roster", type=int, default=None, help="Roster id. Default: yours.")
@click.option("--sims", default=400, show_default=True, help="Simulated paths per decision.")
@click.option(
    "--locked",
    default=None,
    help="What you have already banked, as 'sleeper_id:score,...'. Default: reconstructed.",
)
def explain(
    player: str, as_of: str | None, roster: int | None, sims: int, locked: str | None
) -> None:
    """Why the engine says what it says about one player.

    PLAYER is a sleeper_id or a case-insensitive substring of a name.

    Built from the same :func:`lockin.digest.build` call the digest itself
    renders, rather than recomputing the numbers with a second code path. A
    diagnostic that can disagree with the thing it is diagnosing is worse than
    no diagnostic — so --locked is accepted here too, and must be given the same
    value, or the explanation would describe a different state from the digest.
    """
    cfg = Config.from_env()
    today = clock.today_iso(cfg.timezone)
    as_of = as_of or today
    # The same live/replay rule as `digest`, or a traded player's week — read
    # from today's rosters live, from the box scores in a replay — could
    # differ from the one the digest described.
    live = as_of == today
    now = datetime.now(UTC) if live else None
    banked = _parse_locked(locked)

    with _season(cfg) as conn:
        roster_id = roster or digest_mod.roster_for_user(conn, cfg.user_id)
        if roster_id is None:
            raise click.ClickException(f"no roster for user {cfg.user_id}")
        try:
            ctx = digest_mod.load_context(conn, cfg.season)
            report = digest_mod.build(
                ctx,
                roster_id,
                as_of,
                n_sims=sims,
                n_paths=sims,
                locked=banked,
                live=live,
                now=now,
            )
        except (ValueError, projections_mod.NoGamesYet) as exc:
            raise click.ClickException(str(exc)) from None
        if report.note:
            raise click.ClickException(report.note)

        matches = [
            pid
            for pid, name in report.names.items()
            if pid == player or player.lower() in name.lower()
        ]
        mine = set(ctx.lineup_ids(report.week, roster_id))
        matches = [pid for pid in matches if pid in mine] or matches
        if not matches:
            raise click.ClickException(f"no starter matching {player!r} in week {report.week}")
        if len(matches) > 1:
            names = ", ".join(f"{report.names[p]} ({p})" for p in matches)
            raise click.ClickException(f"{player!r} matches several: {names}")
        pid = matches[0]

        games = digest_mod.lineup_as_of(ctx, [pid], report.week, report.known_through, live=live)[
            pid
        ]
        dist = core_projections.EWMAProjectionSource(
            ctx.panel, verify_mod.scoring_settings(conn)
        ).project(
            pid,
            report.as_of_day,
            fantasy_week=report.week,
            rng=np.random.default_rng(0),
            n_draws=4000,
        )

    name = report.names.get(pid, pid)
    click.echo(f"{name}  ({pid})   week {report.week}, as of {as_of}")
    click.echo(
        f"  roster {report.roster_id} v {report.opponent_roster_id}, P(win) {report.p_win:.1%}"
    )

    click.echo("\n  his week")
    for game in games:
        when = projections_mod.date_of(game.day)
        if game.day <= report.known_through:
            state = f"played {game.score:>6.1f}" if game.played else "did not play"
        else:
            state = "to come"
        click.echo(f"    {when}  {state}")

    if pid in report.banked:
        click.echo(f"\n  LOCKED at {report.banked[pid]:.1f} — nothing left to decide")
        return

    click.echo(
        f"\n  distribution for one game, as of today"
        f"\n    basis {dist.basis} ({dist.n_own_games} prior played games),"
        f" P(DNP) {dist.p_dnp:.1%}"
        f"\n    mean {dist.mean:.1f}   "
        + "  ".join(
            f"q{int(q * 100)} {float(dist.quantile(q)):.0f}" for q in (0.25, 0.5, 0.75, 0.9)
        )
    )

    calls = [c for c in report.calls if c.sleeper_id == pid]
    for call in calls:
        verdict = "LOCK" if call.lock else "PASS"
        click.echo(
            f"\n  last night ({projections_mod.date_of(call.day)}): scored {call.score:.1f}"
            f"\n    {verdict} — P(win) {call.p_win_lock:.1%} locking,"
            f" {call.p_win_pass:.1%} passing"
            f"\n    break-even {call.break_even:.1f}: below it, riding is worth more."
            f"\n    The gap is {call.edge:.2%} of win probability, which is what"
            f"\n    the call is worth — not the {abs(call.score - call.break_even):.1f} points."
        )

    rules = [r for r in report.rules if r.sleeper_id == pid]
    if rules:
        click.echo("\n  standing rules")
        for rule in rules:
            chance = (
                "" if np.isnan(rule.p_clear) else f", he clears it {rule.p_clear:.0%} of the time"
            )
            idle = (
                f"\n      assumes {rule.idle_nights} idle decision night(s) first (§7.2)"
                if rule.idle_nights
                else ""
            )
            click.echo(
                f"    {projections_mod.date_of(rule.night)}: lock if he clears"
                f" {rule.threshold:.0f}{chance}"
                f"\n      {rule.games_after} game(s) left after it{idle}"
            )

    warnings = [w for w in report.warnings if w.sleeper_id == pid]
    for warn in warnings:
        click.echo(f"\n  WARNING — {warn.kind}: {warn.detail}")

    click.echo(
        "\n  Thresholds are win-probability break-evens against THIS opponent in"
        "\n  THIS state, not a view on the player. The same score is a lock when"
        "\n  you are ahead and a pass when you need variance (§4)."
    )


def _render(checks, title: str, as_json: bool) -> None:
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "name": c.name,
                        "passed": c.passed,
                        "detail": c.detail,
                        "offenders": c.offenders,
                    }
                    for c in checks
                ],
                indent=2,
            )
        )
    else:
        click.echo(title)
        click.echo("=" * 60)
        for c in checks:
            click.echo(f"[{'PASS' if c.passed else 'FAIL'}] {c.name}")
            click.echo(f"       {c.detail}")
            for o in c.offenders:
                click.echo(f"         - {o}")
        click.echo("=" * 60)

    failed = [c for c in checks if not c.passed]
    if failed:
        click.echo(f"{len(failed)} gate(s) failed", err=True)
        sys.exit(1)
    click.echo("all gates passed")


if __name__ == "__main__":
    main()
