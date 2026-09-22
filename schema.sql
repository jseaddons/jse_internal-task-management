-- ============================================================================
-- Master Task Log — normalized SQLite schema
-- Internal project 2630 / client M45. Sub-projects 5.2, 5.4, and future ones.
--
-- Design goal: adding a new sub-project (its own buildings / models / master
-- tasks / config) is a DATA change (INSERTs), never a schema change.
--
-- Business rules baked in:
--   * Hours   = (Out - In) - overlapping standard breaks        (in/out drive it)
--   * Man-days= Hours / project.hours_per_day  (per assignee; row total x N)
--   * Permission is a Yes/No client-report flag ONLY — never affects hours.
--   * Levels are NUMERIC only, no Level 0 (enforced by CHECK).
--   * One task per row; a person's tasks must not overlap in a day
--     (cross-row rule — enforced by the v_assignee_overlaps view / importer).
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Projects + per-project working-time config (the reason for the migration)
-- ---------------------------------------------------------------------------
CREATE TABLE projects (
    code           TEXT PRIMARY KEY,               -- '5.2'
    name           TEXT NOT NULL,                  -- 'Dubai-Hotel 5.2 M45'
    client_code    TEXT,                           -- 'M45'
    internal_code  TEXT,                           -- '2630'
    day_start      TEXT NOT NULL DEFAULT '09:00',  -- HH:MM
    day_end        TEXT NOT NULL DEFAULT '18:30',  -- HH:MM (time past this = extra hours)
    hours_per_day  REAL NOT NULL DEFAULT 8.25      -- 1 man-day
    ,no_sheet_type INTEGER NOT NULL DEFAULT 0      -- project does not use Sheet Type
    ,project_status TEXT NOT NULL DEFAULT 'Active'
    ,no_zone INTEGER NOT NULL DEFAULT 0
    ,use_teams INTEGER NOT NULL DEFAULT 0      -- QC Reviewer = team lead, QC Support is assigned
);

-- Standard break windows, per project (auto-deducted only for the overlap
-- with a task's start/end). Defaults: lunch 12:45-13:30, AM 11:15-11:30,
-- PM 16:15-16:30 (= 75 min total).
CREATE TABLE break_windows (
    id            INTEGER PRIMARY KEY,
    project_code  TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
    name          TEXT NOT NULL,                   -- 'Lunch','AM','PM'
    start_time    TEXT NOT NULL,                   -- HH:MM
    end_time      TEXT NOT NULL,
    UNIQUE(project_code, name)
);

-- ---------------------------------------------------------------------------
-- Shared reference tables
-- ---------------------------------------------------------------------------
-- NULL number = 'All levels' sentinel. 0 is the ground floor: AL AIN (24139)
-- numbers from the ground up (F00), while 2630/M45 has no Level 0 by client
-- convention. Levels belong to one project (UNIQUE project + label). A new
-- project starts with none — they are never copied from another project.
-- Basements are negative (B01 = -1, B02 = -2) so they sort below ground.
CREATE TABLE levels (
    id            INTEGER PRIMARY KEY,
    project_code  TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
    number        INTEGER,
    label         TEXT NOT NULL,    -- 'Level 5', 'F00', 'All levels'
    is_all        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_code, label)
);
-- Wording used by an external tracking sheet, mapped onto a sub task we
-- already hold. Keeps an imported sheet's short headings ('Height', 'Clash')
-- from duplicating the catalog. Category is in the key because the same
-- heading can mean different things under different bands.
CREATE TABLE sub_task_aliases (
    source           TEXT NOT NULL,
    category         TEXT NOT NULL,
    alias            TEXT NOT NULL,
    task_sub_task_id INTEGER NOT NULL REFERENCES task_sub_tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (source, category, alias)
);

CREATE TABLE project_levels (
    project_code TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
    level_id INTEGER NOT NULL REFERENCES levels(id) ON DELETE CASCADE,
    PRIMARY KEY(project_code, level_id)
);

CREATE TABLE task_names (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE category_tasks (
    master_task_id INTEGER NOT NULL REFERENCES master_tasks(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    task_name_id INTEGER NOT NULL REFERENCES task_names(id) ON DELETE CASCADE,
    PRIMARY KEY(master_task_id, category_id, task_name_id)
);
CREATE TABLE roles      (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
-- people = the employee record. Name + role are used in the task app; the rest is
-- employee master data.
CREATE TABLE people (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,      -- Emp name
    role_id    INTEGER REFERENCES roles(id),
    emp_code   TEXT,                      -- Emp ID
    category   TEXT,                      -- Emp category
    skillset   TEXT,                      -- Skillset / specialisation
    bim_id     TEXT,
    ip_address TEXT,
    email      TEXT,
    contact    TEXT,
    skill_rating     INTEGER CHECK (skill_rating IS NULL OR skill_rating BETWEEN 1 AND 5),
    experience_years REAL    CHECK (experience_years IS NULL OR experience_years >= 0),
    location         TEXT                          -- 'Chennai', 'Vizag', …
);
CREATE TABLE statuses   (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE sheet_types(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);

-- ---------------------------------------------------------------------------
-- Per-project reference tables (keyed by project_code)
-- ---------------------------------------------------------------------------
CREATE TABLE buildings (
    id            INTEGER PRIMARY KEY,
    project_code  TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
    name          TEXT NOT NULL,                  -- 'Block A', 'B1', 'All Blocks'
    is_all        INTEGER NOT NULL DEFAULT 0,     -- 1 = 'All Blocks' sentinel
    UNIQUE(project_code, name)
);

CREATE TABLE models (
    id            INTEGER PRIMARY KEY,
    project_code  TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    UNIQUE(project_code, name)
);

CREATE TABLE master_tasks (
    id            INTEGER PRIMARY KEY,
    project_code  TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
    sheet_type_id INTEGER REFERENCES sheet_types(id),
    name          TEXT NOT NULL,                  -- full MIDP sheet string (no [NN] prefix)
    UNIQUE(project_code, name)
);

CREATE TABLE sub_tasks (
    id             INTEGER PRIMARY KEY,
    master_task_id INTEGER NOT NULL REFERENCES master_tasks(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    UNIQUE(master_task_id, name)
);

CREATE TABLE sub_sub_tasks (
    id          INTEGER PRIMARY KEY,
    sub_task_id INTEGER NOT NULL REFERENCES sub_tasks(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    UNIQUE(sub_task_id, name)
);

-- Sub Tasks are the bottom of the entry hierarchy. They are scoped to a
-- Category as well as a Task, because the Task level is a fixed three-item
-- list (REVIEW COMMENTS / CHECK MODEL HEALTH / CHECK UPDATES) and the Category
-- is what makes one Sub Task list differ from the next.
CREATE TABLE task_sub_tasks (
    id           INTEGER PRIMARY KEY,
    task_name_id INTEGER NOT NULL REFERENCES task_names(id) ON DELETE CASCADE,
    category_id  INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    UNIQUE(task_name_id, category_id, name)
);

-- ---------------------------------------------------------------------------
-- Fact table: one row = one task (one Sub-Task action)
-- ---------------------------------------------------------------------------
CREATE TABLE tasks (
    id             INTEGER PRIMARY KEY,
    work_date      TEXT NOT NULL,                 -- 'YYYY-MM-DD'
    start_time     TEXT,                          -- 'HH:MM' (In)
    end_time       TEXT,                          -- 'HH:MM' (Out)
    break_mins     REAL NOT NULL DEFAULT 0,       -- typed value trusted if present, else computed
    hours          REAL NOT NULL DEFAULT 0,       -- (Out - In) - break_mins
    permission     INTEGER NOT NULL DEFAULT 0,    -- Yes/No client-report flag only
    grouped        INTEGER NOT NULL DEFAULT 0,    -- 'Group' Yes/No: several varied tasks for one person/day
    project_code   TEXT NOT NULL REFERENCES projects(code),
    sheet_type_id  INTEGER REFERENCES sheet_types(id),
    master_task_id INTEGER REFERENCES master_tasks(id),
    sub_task_id    INTEGER REFERENCES sub_tasks(id),
    sub_sub_task_id INTEGER REFERENCES sub_sub_tasks(id),
    task_sub_task_id INTEGER REFERENCES task_sub_tasks(id),
    building_id    INTEGER REFERENCES buildings(id),
    level_id       INTEGER REFERENCES levels(id),
    model_id       INTEGER REFERENCES models(id),
    task_name_id   INTEGER REFERENCES task_names(id),
    category_id    INTEGER REFERENCES categories(id),
    description    TEXT,
    status_id      INTEGER REFERENCES statuses(id),
    pct_complete   REAL,
    notes          TEXT,
    CHECK (start_time IS NULL OR end_time IS NULL OR length(start_time)=5),
    CHECK (permission IN (0,1)),
    CHECK (grouped IN (0,1))
);

-- Replaces Assignee 1-6. Man-days are derived (hours / hours_per_day) per row.
CREATE TABLE task_assignees (
    task_id   INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    person_id INTEGER NOT NULL REFERENCES people(id),
    PRIMARY KEY (task_id, person_id)
);

-- A person works across several Categories in a day and ticks off several Sub
-- Tasks, so both are many-per-task. tasks.category_id / tasks.task_sub_task_id
-- keep the first tick for anything still reading a single value.
-- Who is on which project, and their role on that project (QC Reviewer,
-- QC Support, etc.). joined_on / left_on are the days membership changed.
-- Who didn't fill includes people from joined_on through left_on. They drop
-- off from the next working day after left_on (Saturday and Sunday skipped).
-- An empty current team means the project offers every employee on the form.
CREATE TABLE project_people (
    id           INTEGER PRIMARY KEY,
    project_code TEXT    NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
    person_id    INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    role_id      INTEGER REFERENCES roles(id),
    joined_on    TEXT    NOT NULL,
    left_on      TEXT
);
CREATE UNIQUE INDEX ix_pp_open ON project_people(project_code, person_id) WHERE left_on IS NULL;

-- QC Support assigned to a QC Reviewer (team lead) on a project.
-- A support sits on one lead's row; a lead can have two or more supports.
CREATE TABLE lead_support (
    project_code TEXT    NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
    lead_id      INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    support_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY (project_code, lead_id, support_id),
    UNIQUE (project_code, support_id)
);

-- Per-project scope for a team lead (QC Reviewer). kind is zone, level, or
-- category (task set). An empty set for a kind means that lead is unrestricted
-- on that axis — mapping varies by project and is never assumed.
CREATE TABLE lead_scope (
    project_code TEXT    NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
    person_id    INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL CHECK (kind IN ('zone','level','category')),
    item_id      INTEGER NOT NULL,
    PRIMARY KEY (project_code, person_id, kind, item_id)
);

CREATE TABLE task_categories (
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, category_id)
);

CREATE TABLE task_sub_task_links (
    task_id          INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    task_sub_task_id INTEGER NOT NULL REFERENCES task_sub_tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, task_sub_task_id)
);

-- ---------------------------------------------------------------------------
-- Indexes for the report views
-- ---------------------------------------------------------------------------
CREATE INDEX ix_tasks_date        ON tasks(work_date);
CREATE INDEX ix_tasks_project     ON tasks(project_code);
CREATE INDEX ix_tasks_master_task ON tasks(master_task_id);
CREATE INDEX ix_tasks_building    ON tasks(building_id);
CREATE INDEX ix_tasks_level       ON tasks(level_id);
CREATE INDEX ix_ta_person         ON task_assignees(person_id);
CREATE INDEX ix_master_tasks_proj ON master_tasks(project_code);
CREATE INDEX ix_sub_tasks_master ON sub_tasks(master_task_id);
CREATE INDEX ix_sub_sub_tasks_sub ON sub_sub_tasks(sub_task_id);
CREATE INDEX ix_task_sub_tasks_task ON task_sub_tasks(task_name_id, category_id);
CREATE INDEX ix_task_categories_cat ON task_categories(category_id);
CREATE INDEX ix_project_people_person ON project_people(person_id);
CREATE INDEX ix_task_sub_task_links_sub ON task_sub_task_links(task_sub_task_id);
CREATE INDEX ix_category_tasks_category ON category_tasks(master_task_id, category_id);
CREATE INDEX ix_buildings_proj    ON buildings(project_code);
CREATE INDEX ix_models_proj       ON models(project_code);

-- ---------------------------------------------------------------------------
-- Forecast / loading plan: assigned future work. Independent of daily `tasks`.
-- Weekly offs are Sunday and the 2nd/4th Saturday. Extra public holidays:
-- ---------------------------------------------------------------------------
CREATE TABLE public_holidays (
    day  TEXT PRIMARY KEY,
    name TEXT
);

-- ---------------------------------------------------------------------------
-- Forecast / loading plan: assigned future work. Independent of daily `tasks`.
-- ---------------------------------------------------------------------------
CREATE TABLE forecast_assignments (
    id               INTEGER PRIMARY KEY,
    work_date        TEXT NOT NULL,
    project_code     TEXT NOT NULL REFERENCES projects(code),
    service          TEXT NOT NULL CHECK (service IN ('Modelling Works', 'Sheet Work')),
    person_id        INTEGER NOT NULL REFERENCES people(id),
    category_id      INTEGER REFERENCES categories(id),
    building_id      INTEGER REFERENCES buildings(id),
    level_id         INTEGER REFERENCES levels(id),
    area             REAL,
    estimated_hours  REAL NOT NULL,
    task_name_id     INTEGER REFERENCES task_names(id),
    task_sub_task_id INTEGER REFERENCES task_sub_tasks(id),
    model_id         INTEGER REFERENCES models(id),
    drawing_name     TEXT,
    drawing_number   TEXT,
    scale            TEXT,
    paper_size       TEXT,
    notes            TEXT,
    entered_by       INTEGER REFERENCES people(id),
    created_at       TEXT
);
CREATE INDEX ix_forecast_proj_date ON forecast_assignments(project_code, work_date);

-- Hours each sub-task should take. Forecast uses this list as the basis for
-- project % completed / % ongoing (latest Daily Task Entry status).
CREATE TABLE forecast_subtask_budget (
    id               INTEGER PRIMARY KEY,
    project_code     TEXT NOT NULL REFERENCES projects(code) ON DELETE CASCADE,
    category_id      INTEGER REFERENCES categories(id),
    task_sub_task_id INTEGER NOT NULL REFERENCES task_sub_tasks(id),
    building_id      INTEGER REFERENCES buildings(id),
    level_id         INTEGER REFERENCES levels(id),
    hours            REAL NOT NULL
);
CREATE UNIQUE INDEX ix_forecast_budget
    ON forecast_subtask_budget(
        project_code, task_sub_task_id,
        IFNULL(building_id, 0), IFNULL(level_id, 0));
