-- Adds the estimated film timeline to the captions (timeline_captions.py):
-- each placed still gets a start-end interval in minutes and a chronological
-- rank, so tmdb-front can show a film's stills in story order.
-- DDL, so voie 2 (rule sql-vps.md). Run once, before loading timeline.sql.
ALTER TABLE T_WC_TMDB_IMAGE_CAPTION
    ADD COLUMN IF NOT EXISTS TIMELINE_START_MIN SMALLINT    NULL COMMENT 'début estimé du plan dans le film, en minutes (NULL : non placé, ex. visuel promotionnel)' AFTER CAPTION_MODEL,
    ADD COLUMN IF NOT EXISTS TIMELINE_END_MIN   SMALLINT    NULL COMMENT 'fin estimée de l''intervalle, en minutes' AFTER TIMELINE_START_MIN,
    ADD COLUMN IF NOT EXISTS TIMELINE_ORDER     SMALLINT    NULL COMMENT 'rang chronologique estimé parmi les images placées du film (1 = première)' AFTER TIMELINE_END_MIN,
    ADD COLUMN IF NOT EXISTS TIMELINE_MODEL     VARCHAR(64) NULL COMMENT 'modèle qui a estimé la chronologie' AFTER TIMELINE_ORDER;
