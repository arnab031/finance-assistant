-- Rolling health signals.
--
-- Each column corresponds to a failure mode this build has actually produced,
-- so a spike is diagnosable rather than merely alarming. Answered = requests
-- that reached a result, which is the right denominator for correctness rates:
-- a clarification or an out-of-coverage refusal is correct behaviour, not a
-- failure, and must not dilute them.

CREATE OR REPLACE VIEW v_health AS
WITH windowed AS (
    SELECT * FROM query_log WHERE created_at > now() - interval '24 hours'
),
answered AS (SELECT * FROM windowed WHERE row_count > 0 AND error IS NULL)
SELECT
    (SELECT COUNT(*) FROM windowed)                                  AS requests,
    (SELECT COUNT(*) FROM answered)                                  AS answered,

    -- correctness
    (SELECT ROUND(AVG((verified)::int)::numeric, 4)
       FROM windowed WHERE verified IS NOT NULL)                     AS verification_pass_rate,
    (SELECT ROUND(AVG((template_used)::int)::numeric, 4) FROM answered) AS template_fallback_rate,

    -- extraction quality
    (SELECT ROUND(AVG((repaired)::int)::numeric, 4) FROM windowed)   AS repair_rate,
    (SELECT ROUND(AVG((coerced)::int)::numeric, 4)  FROM windowed)   AS coercion_rate,
    (SELECT ROUND(AVG((cardinality(sanity_corrected) > 0)::int)::numeric, 4)
       FROM windowed)                                                AS sanity_correction_rate,

    -- interaction behaviour: too high is nagging, too low is silent guessing
    (SELECT ROUND(AVG((clarified)::int)::numeric, 4) FROM windowed)  AS clarify_rate,
    (SELECT ROUND(AVG((intent = 'unsupported')::int)::numeric, 4)
       FROM windowed WHERE intent IS NOT NULL)                       AS unsupported_rate,
    -- Only counts requests where SQL actually RAN and matched nothing. A
    -- coverage refusal ("no data for December 2026") also has row_count = 0,
    -- but it never executed a query - sql_text IS NULL - and it is correct
    -- behaviour. Without this the guardrail working reads as a fault, which is
    -- exactly what this metric did on its first run.
    (SELECT ROUND(AVG((row_count = 0)::int)::numeric, 4)
       FROM windowed WHERE error IS NULL AND sql_text IS NOT NULL)   AS empty_result_rate,

    -- reliability and cost
    (SELECT ROUND(AVG((error IS NOT NULL)::int)::numeric, 4) FROM windowed) AS error_rate,
    (SELECT PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY total_ms) FROM answered)  AS p50_ms,
    (SELECT PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY total_ms) FROM answered) AS p95_ms,
    (SELECT ROUND(AVG(input_tokens + output_tokens)) FROM answered)  AS avg_tokens;

-- Individual requests worth a human's attention, newest first.
CREATE OR REPLACE VIEW v_incidents AS
SELECT id, created_at, thread_id, LEFT(question, 70) AS question,
       CASE
         WHEN error IS NOT NULL                    THEN 'error'
         WHEN verified IS FALSE                    THEN 'unverified_figures'
         WHEN template_used                        THEN 'narration_fallback'
         WHEN coerced                              THEN 'spec_coerced'
         WHEN cardinality(sanity_corrected) > 0    THEN 'model_ignored_prompt'
         WHEN repaired                             THEN 'spec_repaired'
         WHEN row_count = 0 AND sql_text IS NOT NULL THEN 'empty_result'
         WHEN total_ms > 15000                     THEN 'slow'
       END                                          AS issue,
       unverified, sanity_corrected, intent, row_count, total_ms, error
FROM query_log
WHERE error IS NOT NULL OR verified IS FALSE OR template_used OR coerced
   OR cardinality(sanity_corrected) > 0 OR repaired OR total_ms > 15000
   OR (row_count = 0 AND sql_text IS NOT NULL)
ORDER BY created_at DESC;
