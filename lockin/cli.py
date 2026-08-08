"""Command-line surface.

Read-only throughout: the Sleeper API offers no way to act on your behalf, so
every recommendation is executed by hand in the app. `digest` and `backtest`
arrive with the phases that give them something to say.
"""

from __future__ import annotations

import json
import sys

import click
import numpy as np

from lockin import backtest as backtest_mod
from lockin import calibrate as calibrate_mod
from lockin import locks as locks_mod
from lockin import managers as managers_mod
from lockin import projections as projections_mod
from lockin import reconcile as reconcile_mod
from lockin import verify as verify_mod
from lockin.config import ALL_STAT_WEEKS, Config
from lockin.core import projections as core_projections
from lockin.ingest import nba as nba_ingest
from lockin.ingest import sleeper as sleeper_ingest
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


@click.group()
def main() -> None:
    """Sleeper NBA Lock-In lineup engine."""


@main.command()
@click.option("--weeks", default=None, help="Week range, e.g. '1-25' or '12,13'. Default: all.")
@click.option("--full", is_flag=True, help="Refetch the ~2.5MB player reference payload.")
@click.option("--skip-nba", is_flag=True, help="Skip the NBA schedule ingest.")
@click.option("--skip-tipoffs", is_flag=True, help="Skip the per-date tipoff sweep (slow).")
def ingest(weeks: str | None, full: bool, skip_nba: bool, skip_tipoffs: bool) -> None:
    """Refresh league state, box scores, matchups and the NBA schedule."""
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

        have_players = conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]
        if full or not have_players:
            n = sleeper_ingest.ingest_players(conn, client)
            click.echo(f"  players     {n} (live snapshot)")
        else:
            click.echo(f"  players     {have_players} cached (--full to refresh)")

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

    labels: dict[int, str] = {}
    if names:
        client = sleeper_ingest.SleeperClient()
        users = client.get(f"{sleeper_ingest.V1}/league/{cfg.league_id}/users")
        rosters = client.get(f"{sleeper_ingest.V1}/league/{cfg.league_id}/rosters")
        owner_to_roster = {r["owner_id"]: r["roster_id"] for r in rosters}
        for u in users:
            if u["user_id"] in owner_to_roster:
                labels[owner_to_roster[u["user_id"]]] = u.get("display_name", "")

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
