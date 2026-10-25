-- TMDb ids of the films in the Criterion Collection, one per line, for
-- caption_images.py --movie-ids-file. Re-run when the Criterion set grows; the
-- captioner skips rows already captioned, so a rerun only pays for new films.
SELECT ID_MOVIE
FROM T_WC_T2S_MOVIE
WHERE ID_CRITERION IS NOT NULL
  AND (DELETED IS NULL OR DELETED = 0)
ORDER BY ID_MOVIE;
