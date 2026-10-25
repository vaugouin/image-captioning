-- Control after loading captions.sql and timeline.sql: per film, captions,
-- placed stills and timeline span. Read-only (voie 1).
SELECT C.ID_ENTITY, M.TITLE,
       COUNT(*) AS N_CAPTIONS,
       SUM(C.TIMELINE_ORDER IS NOT NULL) AS N_PLACED,
       MIN(C.TIMELINE_START_MIN) AS FIRST_MIN,
       MAX(C.TIMELINE_END_MIN) AS LAST_MIN,
       M.RUNTIME
FROM T_WC_TMDB_IMAGE_CAPTION C
LEFT JOIN T_WC_TMDB_MOVIE M ON M.ID_MOVIE = C.ID_ENTITY
WHERE C.TABLE_SOURCE = 'movie'
GROUP BY C.ID_ENTITY, M.TITLE, M.RUNTIME
ORDER BY C.ID_ENTITY;
