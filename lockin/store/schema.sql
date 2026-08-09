-- Sleeper NBA Lock-In Engine — storage schema
--
-- SQLite is the contract between the ingest job and every reader (architecture
-- doc design rule 2). Readers must never reach past this into an API client.
--
-- Point-in-time discipline: `players` is a LIVE SNAPSHOT and carries no history.
--
-- CORRECTION (2026-08-08): so is box_scores.pit_positions / pit_team. The
-- `player` object Sleeper embeds in each stat row is written at FETCH time, not
-- at game time — identical across weeks 3, 12 and 20 for all 519 players, and a
-- 100% match to today's /players/nba. §3 told readers to prefer pit_* over
-- `players`; that is the same data and offers no protection. See §17.
--
-- The one genuinely point-in-time player attribute is `box_scores.team`, which
-- comes from the stat row itself rather than the embedded object: 104 of 602
-- players changed team mid-season there, against 0 in pit_team.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;


-- ---------------------------------------------------------------- league state

-- Raw league payload, kept whole. scoring_settings is the source of truth for
-- the scoring engine; nothing downstream may hardcode weights.
CREATE TABLE IF NOT EXISTS league_settings (
    league_id       TEXT NOT NULL,
    season          TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (league_id, season)
);

CREATE TABLE IF NOT EXISTS rosters (
    league_id       TEXT NOT NULL,
    roster_id       INTEGER NOT NULL,
    owner_id        TEXT,
    sleeper_id      TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    PRIMARY KEY (league_id, roster_id, sleeper_id, observed_at)
);


-- ------------------------------------------------------------------ reference

-- LIVE SNAPSHOT. Positions and team are as-of `updated_at`, not historical.
-- nba_id is a nullable convenience column; the ID crosswalk is not needed for
-- box scores, which arrive natively keyed by sleeper_id.
CREATE TABLE IF NOT EXISTS players (
    sleeper_id      TEXT PRIMARY KEY,
    nba_id          INTEGER UNIQUE,
    full_name       TEXT NOT NULL,
    positions       TEXT NOT NULL,   -- JSON array of fantasy_positions
    team            TEXT,
    status          TEXT,
    injury_status   TEXT,
    updated_at      TEXT NOT NULL
);

-- NBA-side schedule. Supplies tipoff times, which Sleeper's date-only stat rows
-- do not carry. game_id here is the NBA id space.
CREATE TABLE IF NOT EXISTS nba_schedule (
    nba_game_id     TEXT PRIMARY KEY,
    season          TEXT NOT NULL,
    game_date       TEXT NOT NULL,
    tipoff_utc      TEXT,
    home_team       TEXT NOT NULL,
    away_team       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nba_schedule_date ON nba_schedule (game_date);

-- Sleeper and NBA use different game id spaces, and Sleeper stat rows expose
-- only (team, opponent) with no home/away marker. Linking is therefore on
-- (date, unordered team pair), which is unique: two teams meet at most once a day.
-- `occurred` distinguishes a fixture that was PLAYED from one that was
-- POSTPONED, and the difference is worth real points.
--
-- Sleeper keeps the original fixture row when a game is postponed, with every
-- player unplayed. That looks identical to a DNP unless you check. It is not:
--
--   final scheduled game was REAL and player sat  -> counts 0.0
--   final scheduled fixture was POSTPONED         -> excluded; prior game counts
--
-- Verified in week 12 (2025-26): Jamal Murray's final game was real, he sat,
-- and he counted 0.0 after scoring 61.0 earlier in the week. Bam Adebayo's
-- final fixture (CHI/MIA, 2026-01-08) was postponed to 2026-01-29, and he
-- counted his last played game rather than a zero.
--
-- Derived from Sleeper alone — a fixture where no player recorded a stat line
-- did not happen — and cross-checked against nba_game_id being resolvable.
CREATE TABLE IF NOT EXISTS game_links (
    sleeper_game_id TEXT PRIMARY KEY,
    nba_game_id     TEXT,
    game_date       TEXT NOT NULL,
    team_a          TEXT NOT NULL,   -- lexicographically first of the pair
    team_b          TEXT NOT NULL,
    occurred        INTEGER,         -- 1 played, 0 postponed/cancelled, NULL unknown
    is_exhibition   INTEGER,         -- 1 if not a real NBA fixture (All-Star Game)
    FOREIGN KEY (nba_game_id) REFERENCES nba_schedule (nba_game_id)
);


-- --------------------------------------------------------------- observations

-- One row per player per SCHEDULED game. Rows exist for games the player sat
-- out (played = 0), which is what makes "final game of the week counts, even a
-- 0.0" computable.
--
-- reb is stored despite scoring 0.0 in this league: double-double and
-- triple-double detection needs total rebounds.
--
-- fgmi/ftmi arrive directly from Sleeper AND are derivable as fga-fgm /
-- fta-ftm. Both are stored so ingest can assert they agree.
CREATE TABLE IF NOT EXISTS box_scores (
    sleeper_game_id TEXT NOT NULL,
    sleeper_id      TEXT NOT NULL,
    season          TEXT NOT NULL,
    season_type     TEXT NOT NULL,
    fantasy_week    INTEGER NOT NULL,
    game_date       TEXT NOT NULL,
    team            TEXT,
    opponent        TEXT,
    played          INTEGER NOT NULL,

    -- Sleeper's stat feed carries TEAM AGGREGATE rows alongside players
    -- ("TEAM_OKC": 125 pts, 38 reb, 29 ast). They are not players, never appear
    -- in a lineup, and carry no dd/td — but a naive double-double derivation
    -- reads a team line as a triple-double every night. Kept because team
    -- totals are useful context for the projection layer (pace, usage share),
    -- flagged so nothing scores them.
    is_team_row     INTEGER,

    seconds_played  REAL,
    pts INTEGER, ast INTEGER, oreb INTEGER, dreb INTEGER, reb INTEGER,
    stl INTEGER, blk INTEGER, tov INTEGER,
    fgm INTEGER, fga INTEGER, fgmi INTEGER,
    ftm INTEGER, fta INTEGER, ftmi INTEGER,
    tpm INTEGER, tpa INTEGER, tpmi INTEGER,
    tech INTEGER, flagrant INTEGER, pf INTEGER,
    dd INTEGER, td INTEGER,
    plus_minus REAL,

    -- Embedded player attributes. NOT point-in-time despite the name: Sleeper
    -- writes this object at fetch time, so these are today's values stamped on
    -- a historical row (§17). Kept because they are what we have, and because
    -- deleting them would only hide the problem.
    --
    -- For a player's team as of this game, use `team` above, not pit_team.
    -- There is no equivalent for positions: Sleeper publishes no history, so
    -- `player_status` has to be accumulated live from here on.
    pit_positions   TEXT,            -- JSON array; live snapshot
    pit_team        TEXT,            -- live snapshot; prefer `team`

    dnp_reason      TEXT,
    raw_stats       TEXT NOT NULL,   -- full stats dict, for reprocessing
    ingested_at     TEXT NOT NULL,
    PRIMARY KEY (sleeper_game_id, sleeper_id)
);

CREATE INDEX IF NOT EXISTS idx_box_player_week ON box_scores (sleeper_id, fantasy_week);
CREATE INDEX IF NOT EXISTS idx_box_week ON box_scores (season, fantasy_week);
CREATE INDEX IF NOT EXISTS idx_box_date ON box_scores (game_date);

CREATE TABLE IF NOT EXISTS player_status (
    sleeper_id      TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    designation     TEXT,            -- OUT / DOUBTFUL / QUESTIONABLE / PROBABLE / null
    PRIMARY KEY (sleeper_id, as_of)
);


-- -------------------------------------------------------------- league weekly

-- APPEND-ONLY. Never upsert. The polling history is what lets live opponent
-- lock state be inferred (architecture doc §10) and is irreplaceable after the
-- fact.
--
-- slot_index is the position in the `starters` array, which is what maps a
-- player to PG/G/F/C/UTIL/UTIL. slot is derived from it via roster_positions.
-- matchup_id is NULLABLE. A team not playing a matchup that week has no id:
-- weeks 23-24 exclude the two rosters eliminated from the playoff bracket, and
-- week 25 is unscored entirely (`last_scored_leg: 24`), so every roster is null.
CREATE TABLE IF NOT EXISTS weekly_matchups (
    week            INTEGER NOT NULL,
    roster_id       INTEGER NOT NULL,
    matchup_id      INTEGER,
    sleeper_id      TEXT NOT NULL,
    counted_points  REAL,
    is_starter      INTEGER NOT NULL,
    slot_index      INTEGER,
    slot            TEXT,
    observed_at     TEXT NOT NULL,
    PRIMARY KEY (week, roster_id, sleeper_id, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_matchups_week ON weekly_matchups (week, roster_id);

-- Team-level totals, also append-only. points sums the six starter slots only.
CREATE TABLE IF NOT EXISTS weekly_matchup_teams (
    week            INTEGER NOT NULL,
    roster_id       INTEGER NOT NULL,
    matchup_id      INTEGER,         -- nullable, see weekly_matchups above
    points          REAL,
    custom_points   REAL,
    observed_at     TEXT NOT NULL,
    PRIMARY KEY (week, roster_id, observed_at)
);


-- Latest observation per player-week / team-week.
--
-- weekly_matchups is append-only, so after two ingests every player-week has
-- two rows. Any reader that forgets this double-counts — `SUM(counted_points)`
-- silently returns twice the team's score. Read through these views, never the
-- base tables, unless you specifically want the polling history.
--
-- ISO-8601 `observed_at` sorts lexicographically, so MAX() is the latest.
CREATE VIEW IF NOT EXISTS weekly_matchups_latest AS
SELECT m.*
  FROM weekly_matchups m
  JOIN (
        SELECT week, roster_id, sleeper_id, MAX(observed_at) AS mx
          FROM weekly_matchups
         GROUP BY week, roster_id, sleeper_id
       ) t
    ON t.week = m.week
   AND t.roster_id = m.roster_id
   AND t.sleeper_id = m.sleeper_id
   AND t.mx = m.observed_at;

CREATE VIEW IF NOT EXISTS weekly_matchup_teams_latest AS
SELECT t.*
  FROM weekly_matchup_teams t
  JOIN (
        SELECT week, roster_id, MAX(observed_at) AS mx
          FROM weekly_matchup_teams
         GROUP BY week, roster_id
       ) x
    ON x.week = t.week
   AND x.roster_id = t.roster_id
   AND x.mx = t.observed_at;


-- -------------------------------------------------------------------- derived

-- Phase 2 output. matched_game_index is which game of the week the counted
-- score corresponds to; ambiguous_indices records collisions rather than
-- silently picking one.
CREATE TABLE IF NOT EXISTS lock_inferences (
    week                INTEGER NOT NULL,
    roster_id           INTEGER NOT NULL,
    sleeper_id          TEXT NOT NULL,
    status              TEXT NOT NULL,   -- see core.locks.LockStatus
    n_games             INTEGER NOT NULL,
    matched_game_index  INTEGER,
    locked_game_id      TEXT,
    locked_early        INTEGER,         -- 1 / 0 / NULL when undetermined
    counted_points      REAL,
    ambiguous_indices   TEXT,            -- JSON array, empty when unambiguous
    confidence          REAL NOT NULL,
    PRIMARY KEY (week, roster_id, sleeper_id)
);

-- Per-manager stopping tendency: "locks early and safe" vs "rides to Sunday".
-- Built for all ten rosters, not just ours — the Phase 5 evaluation replays
-- every roster, and the live opponent model needs a profile per manager.
CREATE TABLE IF NOT EXISTS manager_profiles (
    roster_id           INTEGER PRIMARY KEY,
    decisions           INTEGER NOT NULL,
    locked_early        INTEGER NOT NULL,
    rode_to_end         INTEGER NOT NULL,
    lock_rate           REAL NOT NULL,
    mean_lock_position  REAL,
    computed_at         TEXT NOT NULL
);

-- Manager decision quality (`lockin managers`, implementation-plan.md §16).
--
-- Persisted rather than computed on demand because the design rule is that
-- SQLite is the contract and a dashboard is just a second reader. Producing
-- these rows costs a few seconds of Monte Carlo per run, which is fine for a
-- command and far too slow for a page load.
--
-- One row per lock/pass call a manager actually faced. Ambiguous inferences —
-- several games sharing the counted value — are absent rather than guessed, so
-- this table is a subset of lock_inferences, not a join partner for all of it.
--
-- Keyed on decision_day as well as the player: a player can face SEVERAL
-- decisions in one week, one per night he plays with games still to come, so
-- (week, roster, player) is not unique. lock_inferences has one row per
-- player-week because it records the outcome; this records the choices.
--
-- p_win_lock / p_win_pass are the rollout's estimates at that moment, so they
-- reflect the model that produced them. Recomputing after a projection change
-- will move them; that is intended, and `computed_at` is what tells you which
-- model a row came from.
CREATE TABLE IF NOT EXISTS manager_decisions (
    week            INTEGER NOT NULL,
    roster_id       INTEGER NOT NULL,
    sleeper_id      TEXT NOT NULL,
    decision_day    INTEGER NOT NULL, -- proleptic Gregorian ordinal
    score           REAL NOT NULL,   -- what was on the table to bank
    chose_lock      INTEGER NOT NULL,
    p_win_lock      REAL NOT NULL,
    p_win_pass      REAL NOT NULL,
    greedy_locks    INTEGER NOT NULL, -- what the points-only policy would do
    computed_at     TEXT NOT NULL,
    PRIMARY KEY (week, roster_id, sleeper_id, decision_day)
);

CREATE INDEX IF NOT EXISTS idx_manager_decisions_roster ON manager_decisions (roster_id);

-- The ranking a dashboard renders. Sorted on squandered_share ASC, never on
-- upside_share: points capture scores a correct variance-taking decision as a
-- blunder, and carrying it here without that caveat is how it would end up
-- being the column somebody sorts by.
--
-- squandered_share rather than mean_regret because raw regret is
-- P(wrong) x E[stake | wrong], and the second term is circumstance: a hopeless
-- matchup carries a mean stake of 3.0% against 10.4% in a competitive one, so
-- being blown out repeatedly earns low regret for free.
CREATE TABLE IF NOT EXISTS manager_scorecards (
    roster_id            INTEGER PRIMARY KEY,
    decisions            INTEGER NOT NULL,
    squandered_share     REAL NOT NULL,   -- regret / win probability at stake
    mean_stake           REAL NOT NULL,   -- circumstance, not skill
    mean_regret          REAL NOT NULL,   -- win probability forfeited per decision
    right_rate           REAL NOT NULL,
    regret_lo            REAL NOT NULL,   -- bootstrap 90% band; the middle of the
    regret_hi            REAL NOT NULL,   -- table is a tie, and must render as one
    divergent            INTEGER NOT NULL,
    divergent_right_rate REAL NOT NULL,
    upside_share         REAL NOT NULL,   -- points capture, for contrast only
    upside_decisions     INTEGER NOT NULL,
    rode_to_zero         INTEGER NOT NULL,
    computed_at          TEXT NOT NULL
);


-- How good each TEAM was, as distinct from how well it was managed
-- (implementation-plan.md §16). A dashboard wants both, side by side: the
-- interesting question is which teams were well run and which merely good.
--
-- ceiling is the best legal lineup from the whole roster with every lock
-- perfect. realised_ceiling restricts that to the six actually started, so the
-- difference is the price of lineup selection — a decision, not roster quality,
-- and it belongs beside the manager metrics rather than here.
CREATE TABLE IF NOT EXISTS roster_strength (
    roster_id        INTEGER PRIMARY KEY,
    ceiling          REAL NOT NULL,   -- best legal six, perfect locks
    realised_ceiling REAL NOT NULL,   -- the six actually started, perfect locks
    lineup_gap       REAL NOT NULL,   -- ceiling - realised_ceiling
    talent_per_game  REAL NOT NULL,   -- schedule-neutral
    games_per_week   REAL NOT NULL,
    computed_at      TEXT NOT NULL
);


-- Phase 6 output. Present so the read layer has a stable target.
CREATE TABLE IF NOT EXISTS recommendations (
    generated_at    TEXT NOT NULL,
    week            INTEGER NOT NULL,
    sleeper_id      TEXT NOT NULL,
    action          TEXT NOT NULL,   -- LOCK / PASS / START / SIT
    threshold       REAL,
    ev_lock         REAL,
    ev_pass         REAL,
    win_prob_delta  REAL,
    rationale       TEXT,
    PRIMARY KEY (generated_at, week, sleeper_id)
);


-- ------------------------------------------------------------------ bookkeeping

CREATE TABLE IF NOT EXISTS ingest_log (
    source          TEXT NOT NULL,
    target          TEXT NOT NULL,   -- e.g. "stats:week=12"
    rows            INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    PRIMARY KEY (source, target, started_at)
);
