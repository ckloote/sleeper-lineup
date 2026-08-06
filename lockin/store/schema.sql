-- Sleeper NBA Lock-In Engine — storage schema
--
-- SQLite is the contract between the ingest job and every reader (architecture
-- doc design rule 2). Readers must never reach past this into an API client.
--
-- Point-in-time discipline: `players` is a LIVE SNAPSHOT and carries no history.
-- Anything reconstructing a past week must read positions and team from
-- box_scores.pit_positions / pit_team, which are captured per game. See
-- implementation-plan.md §3.

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

    -- Point-in-time player attributes, embedded in the stat row by Sleeper.
    -- Use THESE for historical reconstruction, never players.positions/team.
    pit_positions   TEXT,            -- JSON array
    pit_team        TEXT,

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


-- -------------------------------------------------------------------- derived

-- Phase 2 output. matched_game_index is which game of the week the counted
-- score corresponds to; ambiguous_indices records collisions rather than
-- silently picking one.
CREATE TABLE IF NOT EXISTS lock_inferences (
    week                INTEGER NOT NULL,
    roster_id           INTEGER NOT NULL,
    sleeper_id          TEXT NOT NULL,
    n_games             INTEGER NOT NULL,
    matched_game_index  INTEGER,
    locked_game_id      TEXT,
    ambiguous_indices   TEXT,        -- JSON array, empty when unambiguous
    confidence          REAL NOT NULL,
    PRIMARY KEY (week, roster_id, sleeper_id)
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
