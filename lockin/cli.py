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
"""

from __future__ import annotations

import json
import sys
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
from lockin import serve as serve_mod
from lockin import verify as verify_mod
from lockin.config import ALL_STAT_WEEKS, Config
from lockin.core import projections as core_projections
from lockin.ingest import nba as nba_ingest
from lockin.ingest import sleeper as sleeper_ingest
from lockin.store import db
from lockin.store.db import session


def _parse_weeks(spec: str | None) -> list[int]:
    if not spec:
        return list(ALL_STAT_WEEKS)
    out: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
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
    """Sleeper NBA Lock-In lineup engine."""


@main.command()
@click.option("--weeks", default=None, help="Week range, e.g. '1-25' or '12,13'. Default: all.")
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
    week_list = _parse_weeks(weeks)
    client = sleeper_ingest.SleeperClient()

    with session(cfg.db_path) as conn:
        click.echo(f"league {cfg.league_id} season {cfg.season} -> {cfg.db_path}")

        league = sleeper_ingest.ingest_league(conn, client, cfg.league_id)
        roster_positions = league["roster_positions"]
        click.echo(f"  league      slots={' '.join(roster_positions[:6])}")

        n = sleeper_ingest.ingest_rosters(conn, client, cfg.league_id)
        click.echo(f"  rosters     {n} roster-player rows")

        n = sleeper_ingest.ingest_players(conn, client)
        click.echo(f"  players     {n} (live snapshot)")
        days, status_rows = sleeper_ingest.status_coverage(conn)
        click.echo(f"  status      {status_rows} availability rows across {days} day(s)")

        total_rows = total_played = snapshots_written = 0
        for week in week_list:
            rows, played = sleeper_ingest.ingest_week_stats(conn, client, cfg.season, week)
            _, snap = sleeper_ingest.ingest_matchups(
                conn,
                client,
                cfg.league_id,
                week,
                roster_positions,
                snapshot_root=cfg.snapshot_root,
                season=cfg.season,
            )
            total_rows += rows
            total_played += played
            snapshots_written += 1 if snap else 0
            marker = "  *snapshot changed*" if snap else ""
            click.echo(f"  week {week:>2}     {rows:>5} player-games ({played} played){marker}")
            # Release the write lock between weeks. Holding it for the whole run
            # is what would make a digest firing mid-ingest fail outright.
            db.checkpoint(conn)
        click.echo(f"  box scores  {total_rows} rows, {total_played} played")
        click.echo(
            f"  snapshots   {snapshots_written} new/changed of {len(week_list)} weeks"
            f" -> {cfg.snapshot_root}"
        )

        player_rows, team_rows = sleeper_ingest.refresh_row_kinds(conn)
        click.echo(f"  row kinds   {player_rows} player, {team_rows} team-aggregate")

        occurred, postponed = sleeper_ingest.refresh_game_occurrence(conn)
        click.echo(f"  fixtures    {occurred} played, {postponed} postponed")

        if not skip_nba:
            n = nba_ingest.ingest_schedule(conn, cfg.season)
            click.echo(f"  schedule    {n} NBA games")
            exhibitions = nba_ingest.mark_exhibitions(conn, cfg.season)
            click.echo(f"  exhibitions {exhibitions} non-NBA fixture(s) excluded")

            # Link once to find what is missing, sweep the scoreboard to fill
            # tipoffs and backfill non-regular-season games, then link again.
            nba_ingest.link_games(conn, cfg.season)
            if not skip_tipoffs:
                filled, non_rs = nba_ingest.ingest_scoreboard(conn, cfg.season)
                click.echo(f"  tipoffs     {filled} filled, {non_rs} non-regular-season game(s)")
            linked, unlinked = nba_ingest.link_games(conn, cfg.season)
            click.echo(f"  game links  {linked} linked, {unlinked} unlinked")

    click.echo("done. run `lockin reconcile` to check the Phase 0 gates.")


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def reconcile(as_json: bool) -> None:
    """Report on ingest completeness. Exits nonzero if a gate fails."""
    cfg = Config.from_env()
    with session(cfg.db_path) as conn:
        checks = reconcile_mod.run(conn, cfg.season, cfg.snapshot_root)

    _render(checks, "Phase 0 reconciliation", as_json)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def verify(as_json: bool) -> None:
    """Prove the scoring engine against the recorded season. Nonzero on failure."""
    cfg = Config.from_env()
    with session(cfg.db_path) as conn:
        checks = verify_mod.run(conn, cfg.season)

    _render(checks, "Phase 1 scoring verification", as_json)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.option("--profiles", is_flag=True, help="Print each manager's lock tendency.")
def locks(as_json: bool, profiles: bool) -> None:
    """Recover every manager's lock decisions from the recorded season."""
    cfg = Config.from_env()
    with session(cfg.db_path) as conn:
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
def calibrate(as_json: bool, draws: int, holdout_from: int) -> None:
    """Check the projection layer's quantiles against what happened. Nonzero on failure."""
    cfg = Config.from_env()
    with session(cfg.db_path) as conn:
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
    with session(cfg.db_path) as conn:
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


def _manager_labels(cfg) -> dict[int, str]:
    """Display names, which live only in the API — no table stores them."""
    client = sleeper_ingest.SleeperClient()
    users = client.get(f"{sleeper_ingest.V1}/league/{cfg.league_id}/users")
    payload = client.get(f"{sleeper_ingest.V1}/league/{cfg.league_id}/rosters")
    owner_to_roster = {r["owner_id"]: r["roster_id"] for r in payload}
    return {
        owner_to_roster[u["user_id"]]: u.get("display_name", "")
        for u in users
        if u["user_id"] in owner_to_roster
    }


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
@click.option("--names", is_flag=True, help="Fetch manager display names from Sleeper.")
def teams(as_json: bool, names: bool) -> None:
    """Rank teams on roster quality — how good the side was, not how it was run."""
    cfg = Config.from_env()
    with session(cfg.db_path) as conn:
        strengths = managers_mod.evaluate_rosters(conn, cfg.season)
        managers_mod.persist_rosters(conn, strengths)

    labels = _manager_labels(cfg) if names else {}
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
@click.option("--names", is_flag=True, help="Fetch manager display names from Sleeper.")
@click.option(
    "--competitive",
    is_flag=True,
    help="Only decisions with the matchup live (P(win) 30-70%), matching everyone on difficulty.",
)
def managers(as_json: bool, sims: int, names: bool, competitive: bool) -> None:
    """Rank the managers on decision quality, holding roster talent constant."""
    cfg = Config.from_env()
    with session(cfg.db_path) as conn:
        report = managers_mod.evaluate_managers(
            conn, cfg.season, n_sims=sims, competitive_only=competitive
        )
        n_decisions, n_cards = managers_mod.persist(conn, report)
        strengths = managers_mod.evaluate_rosters(conn, cfg.season)
        managers_mod.persist_rosters(conn, strengths)

    labels = _manager_labels(cfg) if names else {}

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
    as_of = as_of or clock.today_iso(cfg.timezone)
    banked = _parse_locked(locked)

    with session(cfg.db_path) as conn:
        roster_id = roster or digest_mod.roster_for_user(conn, cfg.user_id)
        if roster_id is None:
            raise click.ClickException(
                f"no roster for user {cfg.user_id}; run `lockin ingest` or pass --roster"
            )
        ctx = digest_mod.load_context(conn, cfg.season)
        try:
            report = digest_mod.build(
                ctx, roster_id, as_of, n_sims=sims, n_paths=sims, locked=banked
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
                    "calls": [
                        {
                            "player": c.name,
                            "sleeper_id": c.sleeper_id,
                            "date": projections_mod.date_of(c.day),
                            "score": c.score,
                            "action": "LOCK" if c.lock else "PASS",
                            "break_even": c.break_even,
                            "p_win_lock": c.p_win_lock,
                            "p_win_pass": c.p_win_pass,
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
        source = (
            "banked state as given on the command line"
            if banked is not None
            else "banked state assumes you followed this engine so far — the\n"
            "  noisiest number here; pass --locked when you know it (§20).\n"
            "  Live it comes from the poll history, which does not exist yet (§15)"
        )
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
    with session(cfg.db_path) as conn:
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
    with session(cfg.db_path) as conn:
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
    with session(cfg.db_path) as conn:
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
@click.option("--names", is_flag=True, help="Fetch manager display names from Sleeper.")
def dashboard(out: Path, names: bool) -> None:
    """Render the manager-quality page from what `lockin managers` stored.

    A second reader of SQLite, never a second computation: the Monte Carlo behind
    these rows costs seconds, which is fine for a command and hopeless for a page
    load.
    """
    cfg = Config.from_env()
    with session(cfg.db_path) as conn:
        labels = _manager_labels(cfg) if names else {}
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
    as_of = as_of or clock.today_iso(cfg.timezone)
    banked = _parse_locked(locked)

    with session(cfg.db_path) as conn:
        roster_id = roster or digest_mod.roster_for_user(conn, cfg.user_id)
        if roster_id is None:
            raise click.ClickException(f"no roster for user {cfg.user_id}")
        ctx = digest_mod.load_context(conn, cfg.season)
        try:
            report = digest_mod.build(
                ctx, roster_id, as_of, n_sims=sims, n_paths=sims, locked=banked
            )
        except ValueError as exc:
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

        games = digest_mod.lineup_as_of(
            ctx.panel, ctx.scores, [pid], report.week, report.known_through
        )[pid]
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
