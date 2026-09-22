-- ============================================================================
-- Report views — reproduce the four Excel report tabs, plus validation views.
--
-- Man-day math: 1 man-day = project.hours_per_day (8.25 hrs: 09:00–18:30 minus
-- 75 min breaks). Time past 18:30 is extra hours and extra man-days.
--
-- When several tasks copy the same in/out window (Group), hours are counted
-- once for that person-day and split across those tasks. Sequential windows
-- in one day are added.
--
--   spent_hours  = this row's share of the person-day's actual hours
--   raw_manday   = stored task hours / hours_per_day          (per assignee)
--   norm_manday  = spent_hours / hours_per_day
-- ============================================================================

DROP VIEW IF EXISTS v_employees;
DROP VIEW IF EXISTS v_task_effort;
DROP VIEW IF EXISTS v_daily_report;
DROP VIEW IF EXISTS v_manday_summary;
DROP VIEW IF EXISTS v_search;
DROP VIEW IF EXISTS v_internal_report;
DROP VIEW IF EXISTS v_permission_report;
DROP VIEW IF EXISTS v_assignee_overlaps;
DROP VIEW IF EXISTS v_report_flat;

-- ---------------------------------------------------------------------------
-- Employee master (people + role). Name + role feed the task app; the rest is
-- employee data.
-- ---------------------------------------------------------------------------
CREATE VIEW v_employees AS
SELECT pe.emp_code, pe.name AS emp_name, r.name AS role, pe.category,
       pe.skillset, pe.skill_rating, pe.experience_years, pe.location,
       pe.bim_id, pe.ip_address, pe.email, pe.contact
FROM people pe LEFT JOIN roles r ON r.id = pe.role_id
ORDER BY pe.name;

-- ---------------------------------------------------------------------------
-- Base view: one row per (task, assignee), fully described, with raw + norm.
-- All report views build on this.
-- ---------------------------------------------------------------------------
CREATE VIEW v_task_effort AS
SELECT
    t.id                       AS task_id,
    t.work_date                AS work_date,
    t.start_time               AS start_time,
    t.end_time                 AS end_time,
    pe.name                    AS person,
    pe.location                AS location,
    ta.person_id               AS person_id,
    t.project_code             AS project_code,
    pr.name                    AS project_name,
    b.name                     AS building,
    l.label                    AS level,
    l.number                   AS level_number,
    s.name                     AS sheet_type,
    m.name                     AS master_task,
    mo.name                    AS model,
    -- Category and Sub Task are many-per-task, so report them as a list.
    -- The single-column fallback keeps rows logged before the switch readable.
    COALESCE((SELECT GROUP_CONCAT(c2.name, ', ')
                FROM task_categories tc JOIN categories c2 ON c2.id = tc.category_id
               WHERE tc.task_id = t.id), cat.name)   AS category,
    tn.name                    AS task_name,
    COALESCE((SELECT GROUP_CONCAT(s2.name, ', ')
                FROM task_sub_task_links tl JOIN task_sub_tasks s2 ON s2.id = tl.task_sub_task_id
               WHERE tl.task_id = t.id), ts.name)    AS sub_task,
    t.description              AS description,
    st.name                    AS status,
    t.pct_complete             AS pct_complete,
    t.permission               AS permission,
    t.grouped                  AS grouped,
    t.notes                    AS notes,
    t.break_mins               AS break_mins,
    t.hours                    AS hours,
    pr.hours_per_day           AS hours_per_day,
    CASE WHEN x.day_sum_hours > 0
         THEN t.hours / x.day_sum_hours * x.actual_day_hours
         ELSE 0 END            AS spent_hours,
    (t.hours / pr.hours_per_day) AS raw_manday,
    CASE WHEN x.day_sum_hours > 0 AND pr.hours_per_day > 0
         THEN (t.hours / x.day_sum_hours * x.actual_day_hours) / pr.hours_per_day
         ELSE 0 END            AS norm_manday
FROM task_assignees ta
JOIN tasks       t  ON t.id  = ta.task_id
JOIN people      pe ON pe.id = ta.person_id
JOIN projects    pr ON pr.code = t.project_code
JOIN (
    SELECT
        person_id,
        work_date,
        SUM(win_sum)   AS day_sum_hours,
        SUM(win_hours) AS actual_day_hours
    FROM (
        SELECT
            ta2.person_id AS person_id,
            t2.work_date  AS work_date,
            MAX(t2.hours) AS win_hours,
            SUM(t2.hours) AS win_sum
        FROM task_assignees ta2
        JOIN tasks t2 ON t2.id = ta2.task_id
        GROUP BY ta2.person_id, t2.work_date,
                 COALESCE(t2.start_time,''), COALESCE(t2.end_time,'')
    )
    GROUP BY person_id, work_date
) x ON x.person_id = ta.person_id AND x.work_date = t.work_date
LEFT JOIN buildings    b  ON b.id  = t.building_id
LEFT JOIN levels       l  ON l.id  = t.level_id
LEFT JOIN sheet_types  s  ON s.id  = t.sheet_type_id
LEFT JOIN master_tasks m  ON m.id  = t.master_task_id
LEFT JOIN models       mo ON mo.id = t.model_id
LEFT JOIN task_names   tn ON tn.id = t.task_name_id
LEFT JOIN categories   cat ON cat.id = t.category_id
LEFT JOIN task_sub_tasks ts ON ts.id = t.task_sub_task_id
LEFT JOIN statuses     st ON st.id = t.status_id;

-- ---------------------------------------------------------------------------
-- 1) Daily Report — one date -> that day's tasks, per person, man-days.
--    Filter: WHERE work_date = '2026-08-04'
-- ---------------------------------------------------------------------------
CREATE VIEW v_daily_report AS
SELECT
    work_date,
    person,
    project_code,
    building,
    level,
    sheet_type,
    master_task,
    model,
    category,
    task_name,
    sub_task,
    description,
    status,
    permission,
    grouped,
    ROUND(spent_hours, 2) AS hours,
    ROUND(norm_manday, 3) AS man_days
FROM v_task_effort
ORDER BY work_date, person, start_time;

-- ---------------------------------------------------------------------------
-- 2) Man-day Summary — effort rolled up, broken down by person and task type.
--    Filterable: add WHERE on project_code / building / level / sheet_type /
--    master_task / person / task_name. Re-aggregate freely, e.g.
--      SELECT person, SUM(man_days) FROM v_manday_summary GROUP BY person;
-- ---------------------------------------------------------------------------
CREATE VIEW v_manday_summary AS
SELECT
    project_code,
    building,
    level,
    sheet_type,
    master_task,
    category,
    task_name,
    sub_task,
    person,
    COUNT(*)              AS task_count,
    ROUND(SUM(spent_hours), 2)  AS hours,
    ROUND(SUM(norm_manday), 3) AS man_days
FROM v_task_effort
GROUP BY project_code, building, level, sheet_type, master_task, category, task_name, sub_task, person;

-- ---------------------------------------------------------------------------
-- 3) Search — who did what / where / when. Filter by any column.
--    e.g. WHERE person='Aasin' AND task_name='Tagging'
--         WHERE building='B1' AND work_date BETWEEN '2026-08-01' AND '2026-08-31'
-- ---------------------------------------------------------------------------
CREATE VIEW v_search AS
SELECT
    work_date,
    start_time,
    end_time,
    person,
    project_code,
    building,
    level,
    sheet_type,
    master_task,
    model,
    category,
    task_name,
    sub_task,
    description,
    status,
    permission,
    ROUND(spent_hours, 2) AS hours,
    ROUND(norm_manday, 3) AS man_days,
    notes
FROM v_task_effort
ORDER BY work_date, person, start_time;

-- ---------------------------------------------------------------------------
-- 4) Internal Report — effort between a From/To date range.
--    Filter: WHERE work_date BETWEEN '2026-08-01' AND '2026-08-31'
--    (rolled up per date / person / project; re-aggregate as needed)
-- ---------------------------------------------------------------------------
CREATE VIEW v_internal_report AS
SELECT
    work_date,
    person,
    project_code,
    building,
    level,
    sheet_type,
    task_name,
    COUNT(*)              AS task_count,
    ROUND(SUM(spent_hours), 2)  AS hours,
    ROUND(SUM(norm_manday), 3) AS man_days
FROM v_task_effort
GROUP BY work_date, person, project_code, building, level, sheet_type, task_name;

-- ---------------------------------------------------------------------------
-- Client permission report — per person per day: did they leave early (flag),
-- and their actual first-in / last-out. Permission never affects hours.
-- ---------------------------------------------------------------------------
CREATE VIEW v_permission_report AS
SELECT
    work_date,
    person,
    MIN(start_time)          AS first_in,
    MAX(end_time)            AS last_out,
    MAX(permission)          AS permission,
    GROUP_CONCAT(DISTINCT notes) AS notes
FROM v_task_effort
GROUP BY work_date, person
HAVING MAX(permission) = 1;

-- ---------------------------------------------------------------------------
-- Validation — a person's task times overlapping within a day (should be none
-- once the log is entered with partitioned per-task times).
-- ---------------------------------------------------------------------------
CREATE VIEW v_assignee_overlaps AS
SELECT
    a.person_id,
    pe.name        AS person,
    a.work_date,
    a.task_id      AS task_a,
    a.start_time   AS start_a,
    a.end_time     AS end_a,
    b.task_id      AS task_b,
    b.start_time   AS start_b,
    b.end_time     AS end_b
FROM v_task_effort a
JOIN v_task_effort b
  ON a.person_id = b.person_id
 AND a.work_date = b.work_date
 AND a.task_id   < b.task_id
 AND a.start_time IS NOT NULL AND a.end_time IS NOT NULL
 AND b.start_time IS NOT NULL AND b.end_time IS NOT NULL
 AND a.start_time < b.end_time
 AND b.start_time < a.end_time
JOIN people pe ON pe.id = a.person_id
ORDER BY a.work_date, person, a.start_time;

-- ---------------------------------------------------------------------------
-- 10) v_report_flat — the single fact table for Power BI.
--
--     One row per (task x assignee x sub task). Everything is already joined
--     and named, so Power BI imports this one view and pivots it freely; no
--     Power Query modelling needed.
--
--     v_task_effort GROUP_CONCATs category and sub task into comma lists,
--     which reads well in Excel but cannot be sliced. Here they are exploded
--     into rows instead — the whole point of a BI fact table.
--
--     DOUBLE-COUNTING: a task with 3 sub tasks becomes 3 rows, so `hours`
--     repeats on each. Sum `hours_split` (hours divided evenly across the
--     task's sub tasks), never `hours`. Both are exposed: `hours` for
--     filtering to a single row, `hours_split` for totals.
-- ---------------------------------------------------------------------------
CREATE VIEW v_report_flat AS
WITH sub_count AS (
    SELECT task_id, COUNT(*) AS n
      FROM task_sub_task_links
     GROUP BY task_id
)
SELECT
    t.id                          AS task_id,
    t.work_date                   AS work_date,
    CAST(strftime('%Y', t.work_date) AS INTEGER) AS work_year,
    CAST(strftime('%m', t.work_date) AS INTEGER) AS work_month,
    strftime('%Y-%m', t.work_date) AS work_ym,
    pe.name                       AS person,
    pe.emp_code                   AS emp_code,
    r.name                        AS role,
    t.project_code                AS project_code,
    pr.name                       AS project_name,
    b.name                        AS zone,
    l.label                       AS level,
    l.number                      AS level_number,
    mo.name                       AS model,
    sh.name                       AS sheet_type,
    mt.name                       AS master_task,
    tn.name                       AS task_name,
    COALESCE(cs.name, ct.name)    AS category,
    ts.name                       AS sub_task,
    COALESCE(st.name, 'Not Started') AS status,
    t.pct_complete                AS pct_complete,
    t.hours                       AS hours,
    t.hours / MAX(1, COALESCE(sc.n, 1))            AS hours_split,
    (t.hours / pr.hours_per_day)                   AS manday,
    (t.hours / pr.hours_per_day) / MAX(1, COALESCE(sc.n, 1)) AS manday_split,
    eb.name                       AS entered_by,
    t.description                 AS description,
    t.notes                       AS notes
FROM task_assignees ta
JOIN tasks    t  ON t.id   = ta.task_id
JOIN people   pe ON pe.id  = ta.person_id
JOIN projects pr ON pr.code = t.project_code
LEFT JOIN sub_count sc ON sc.task_id = t.id
LEFT JOIN roles        r  ON r.id  = pe.role_id
LEFT JOIN buildings    b  ON b.id  = t.building_id
LEFT JOIN levels       l  ON l.id  = t.level_id
LEFT JOIN models       mo ON mo.id = t.model_id
LEFT JOIN sheet_types  sh ON sh.id = t.sheet_type_id
LEFT JOIN master_tasks mt ON mt.id = t.master_task_id
LEFT JOIN task_names   tn ON tn.id = t.task_name_id
LEFT JOIN task_sub_task_links tl ON tl.task_id = t.id
LEFT JOIN task_sub_tasks ts ON ts.id = tl.task_sub_task_id
LEFT JOIN categories   cs ON cs.id = ts.category_id
LEFT JOIN categories   ct ON ct.id = t.category_id
LEFT JOIN statuses     st ON st.id = t.status_id
LEFT JOIN people       eb ON eb.id = t.entered_by;
