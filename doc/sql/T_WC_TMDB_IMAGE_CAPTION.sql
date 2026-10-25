-- Captions ancrées des images TMDb (backdrops en premier), produites par un
-- modèle de vision Claude. La caption sert à ranger l'image pour la récupération
-- (embedding ChromaDB), jamais à fonder une affirmation.
--
-- DDL de référence. À appliquer sur le VPS par la voie 2 (règle sql-vps.md) :
--   cd ~/docker/<clone>
--   git pull
--   ~/docker/tools/runsqlvaugouindb.sh ~/docker/<clone>/doc/sql/T_WC_TMDB_IMAGE_CAPTION.sql
--
-- Conventions : colonnes UPPERCASE snake_case, PK surrogate ID_*, TIM_* pour les
-- timestamps, clé unique UK_* (voir download-images/AGENTS.md).

CREATE TABLE IF NOT EXISTS T_WC_TMDB_IMAGE_CAPTION (
    ID_IMAGE_CAPTION  BIGINT       NOT NULL AUTO_INCREMENT,
    TABLE_SOURCE      VARCHAR(16)  NOT NULL COMMENT 'movie | serie | season | episode | ...',
    ID_ROW            BIGINT       NOT NULL COMMENT 'ID_ROW de la ligne T_WC_TMDB_*_IMAGE source',
    ID_ENTITY         BIGINT       NOT NULL COMMENT 'ID_MOVIE, ID_SERIE, ... selon TABLE_SOURCE',
    IMAGE_PATH        VARCHAR(255) NOT NULL COMMENT 'chemin TMDb, ex. /abc.jpg',
    CAPTION           TEXT         NOT NULL COMMENT 'phrase descriptive ancrée',
    CAPTION_MODEL     VARCHAR(64)  NOT NULL COMMENT 'ex. claude-sonnet-5-5',
    TIMELINE_START_MIN SMALLINT    NULL     COMMENT 'début estimé du plan dans le film, en minutes (NULL : non placé, ex. visuel promotionnel)',
    TIMELINE_END_MIN   SMALLINT    NULL     COMMENT 'fin estimée de l''intervalle, en minutes',
    TIMELINE_ORDER     SMALLINT    NULL     COMMENT 'rang chronologique estimé parmi les images placées du film (1 = première)',
    TIMELINE_MODEL     VARCHAR(64) NULL     COMMENT 'modèle qui a estimé la chronologie',
    TIM_CREATED       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    TIM_UPDATED       TIMESTAMP    NULL     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ID_IMAGE_CAPTION),
    UNIQUE KEY UK_IMAGE_CAPTION (TABLE_SOURCE, ID_ROW, CAPTION_MODEL),
    KEY ID_ENTITY (ID_ENTITY),
    KEY TABLE_SOURCE (TABLE_SOURCE)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
