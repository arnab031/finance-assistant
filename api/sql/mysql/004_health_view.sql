-- Rolling health signals and the model bake-off table.
--
-- Answered = requests that reached a result, which is the right denominator for
-- correctness rates: a clarification or an out-of-coverage refusal is correct
-- behaviour, not a failure, and must not dilute them.
--
-- Three Postgres constructs have no MySQL equivalent and are rewritten here:
--   PERCENTILE_DISC(x) WITHIN GROUP (ORDER BY c)
--       -> MIN(c) over rows whose CUME_DIST() >= x. That IS the definition of
--          discrete percentile, so the values are identical, not approximated.
--   COUNT(*) FILTER (WHERE p)   -> SUM(CASE WHEN p THEN 1 ELSE 0 END)
--   SELECT DISTINCT ON (model)  -> ROW_NUMBER() OVER (PARTITION BY model ...)
--   cardinality(text[])         -> JSON_LENGTH(json)
--
-- MySQL comparisons already yield 1/0, so AVG(<predicate>) gives the rate
-- directly and the ::int casts Postgres needed simply disappear.

DROP VIEW IF EXISTS v_health;
CREATE VIEW v_health AS
WITH windowed AS (
    SELECT * FROM query_log WHERE created_at > NOW(6) - INTERVAL 24 HOUR
),
answered AS (
    SELECT * FROM windowed WHERE row_count > 0 AND error IS NULL
),
lat AS (
    SELECT total_ms, CUME_DIST() OVER (ORDER BY total_ms) AS cd FROM answered
),
-- The confidence label the pipeline already assigns per request. It lives on
-- chat_messages, not query_log, so reaching it is a join.
--
-- Joined to `answered`, not `windowed`, on purpose. 173 of the high-confidence
-- rows on this database never ran SQL at all - they are unsupported declines
-- and coverage refusals, which are correct behaviour rather than confident
-- answers. Averaging them in would inflate the metric with non-answers, which
-- is exactly the mistake empty_result_rate made on its first run.
conf AS (
    SELECT m.confidence
    FROM chat_messages m
    JOIN answered a ON a.id = m.query_log_id
    WHERE m.role = 'assistant'
)
SELECT
    (SELECT COUNT(*) FROM windowed) AS requests,
    (SELECT COUNT(*) FROM answered) AS answered,

    -- correctness
    (SELECT ROUND(AVG(verified), 4)
       FROM windowed WHERE verified IS NOT NULL)              AS verification_pass_rate,
    (SELECT ROUND(AVG(template_used), 4) FROM answered)       AS template_fallback_rate,

    -- extraction quality
    (SELECT ROUND(AVG(repaired), 4) FROM windowed)            AS repair_rate,
    (SELECT ROUND(AVG(coerced), 4)  FROM windowed)            AS coercion_rate,
    (SELECT ROUND(AVG(JSON_LENGTH(sanity_corrected) > 0), 4)
       FROM windowed)                                         AS sanity_correction_rate,

    -- interaction behaviour: too high is nagging, too low is silent guessing
    (SELECT ROUND(AVG(clarified), 4) FROM windowed)           AS clarify_rate,
    (SELECT ROUND(AVG(intent = 'unsupported'), 4)
       FROM windowed WHERE intent IS NOT NULL)                AS unsupported_rate,
    -- Only counts requests where SQL actually RAN and matched nothing. A
    -- coverage refusal ("no data for December 2026") also has row_count = 0,
    -- but it never executed a query - sql_text IS NULL - and it is correct
    -- behaviour. Without this the guardrail working reads as a fault, which is
    -- exactly what this metric did on its first run.
    (SELECT ROUND(AVG(row_count = 0), 4)
       FROM windowed WHERE error IS NULL AND sql_text IS NOT NULL)
                                                              AS empty_result_rate,

    -- how sure the pipeline was, per answer. Reported as rates per label rather
    -- than an average: the labels are ordinal, not numeric, and inventing a
    -- high=1.0/medium=0.5/low=0.0 mapping would put made-up arithmetic behind a
    -- number people read as measured.
    (SELECT ROUND(AVG(confidence = 'high'), 4)   FROM conf) AS high_confidence_rate,
    (SELECT ROUND(AVG(confidence = 'medium'), 4) FROM conf) AS medium_confidence_rate,
    (SELECT ROUND(AVG(confidence = 'low'), 4)    FROM conf) AS low_confidence_rate,

    -- reliability and cost
    (SELECT ROUND(AVG(error IS NOT NULL), 4) FROM windowed)   AS error_rate,
    (SELECT MIN(total_ms) FROM lat WHERE cd >= 0.50)          AS p50_ms,
    (SELECT MIN(total_ms) FROM lat WHERE cd >= 0.95)          AS p95_ms,
    (SELECT ROUND(AVG(input_tokens + output_tokens)) FROM answered) AS avg_tokens;

-- Individual requests worth a human's attention, newest first.
DROP VIEW IF EXISTS v_incidents;
CREATE VIEW v_incidents AS
SELECT id, created_at, thread_id, LEFT(question, 70) AS question,
       CASE
         WHEN error IS NOT NULL                      THEN 'error'
         WHEN verified IS FALSE                      THEN 'unverified_figures'
         WHEN template_used                          THEN 'narration_fallback'
         WHEN coerced                                THEN 'spec_coerced'
         WHEN JSON_LENGTH(sanity_corrected) > 0      THEN 'model_ignored_prompt'
         WHEN repaired                               THEN 'spec_repaired'
         WHEN row_count = 0 AND sql_text IS NOT NULL THEN 'empty_result'
         WHEN total_ms > 15000                       THEN 'slow'
       END                                            AS issue,
       unverified, sanity_corrected, intent, row_count, total_ms, error
FROM query_log
WHERE error IS NOT NULL OR verified IS FALSE OR template_used OR coerced
   OR JSON_LENGTH(sanity_corrected) > 0 OR repaired OR total_ms > 15000
   OR (row_count = 0 AND sql_text IS NOT NULL)
ORDER BY created_at DESC;

-- One row per model: the bake-off table, straight from measurement.
--
-- Postgres put the per-run percentile and grade counts in correlated scalar
-- subqueries. MySQL cannot correlate into a derived table without LATERAL, so
-- both are aggregated per run up front and joined - which is also cheaper,
-- since each scans eval_results once instead of once per model.
DROP VIEW IF EXISTS v_model_scorecard;
CREATE VIEW v_model_scorecard AS
WITH ranked AS (
    SELECT model, run_id, started_at, n_total, n_passed, duration_ms,
           ROW_NUMBER() OVER (PARTITION BY model ORDER BY started_at DESC) AS rn
    FROM eval_runs WHERE finished_at IS NOT NULL
),
run_lat AS (
    SELECT run_id, latency_ms,
           CUME_DIST() OVER (PARTITION BY run_id ORDER BY latency_ms) AS cd
    FROM eval_results
),
run_p50 AS (
    SELECT run_id, MIN(latency_ms) AS p50_ms
    FROM run_lat WHERE cd >= 0.50 GROUP BY run_id
),
run_grades AS (
    SELECT run_id,
           SUM(CASE WHEN grade='numeric'   AND passed THEN 1 ELSE 0 END) AS numeric_passed,
           SUM(CASE WHEN grade='numeric'                THEN 1 ELSE 0 END) AS numeric_total,
           SUM(CASE WHEN grade='behaviour' AND passed THEN 1 ELSE 0 END) AS behaviour_passed,
           SUM(CASE WHEN grade='behaviour'              THEN 1 ELSE 0 END) AS behaviour_total,
           SUM(CASE WHEN grade='spec'      AND passed THEN 1 ELSE 0 END) AS spec_passed,
           SUM(CASE WHEN grade='spec'                   THEN 1 ELSE 0 END) AS spec_total
    FROM eval_results GROUP BY run_id
)
SELECT l.model, l.run_id, l.started_at, l.n_total, l.n_passed,
       ROUND(l.n_passed / NULLIF(l.n_total, 0), 4) AS accuracy,
       l.duration_ms,
       p.p50_ms,
       g.numeric_passed, g.numeric_total,
       g.behaviour_passed, g.behaviour_total,
       g.spec_passed, g.spec_total
FROM ranked l
LEFT JOIN run_p50   p ON p.run_id = l.run_id
LEFT JOIN run_grades g ON g.run_id = l.run_id
WHERE l.rn = 1
-- MySQL sorts NULLs last on DESC already, so no NULLS LAST clause is needed.
ORDER BY accuracy DESC;
