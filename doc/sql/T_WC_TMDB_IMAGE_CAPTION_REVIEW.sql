-- Jugements humains sur les captions : une ligne par image, par expérience et par
-- relecteur. Une comparaison A/B garde le texte des deux captions jugées (elles ne
-- sont pas forcément dans T_WC_TMDB_IMAGE_CAPTION : une variante perdante n'y entre
-- jamais), pour que le verdict reste lisible après coup.
--
-- VERDICT : A | B | EQUAL | BOTH_WRONG (A/B), ou OK | WRONG pour un jugement simple
-- d'une seule caption (CAPTION_B et VARIANT_B restent alors NULL).
--
-- DDL de référence. À appliquer sur le VPS par la voie 2 (règle sql-vps.md) :
--   cd ~/docker/image-captioning
--   git pull
--   ~/docker/tools/runsqlvaugouindb.sh ~/docker/image-captioning/doc/sql/T_WC_TMDB_IMAGE_CAPTION_REVIEW.sql
--
-- Le chargement des jugements passe par review_votes.py, qui écrit reviews.sql.

CREATE TABLE IF NOT EXISTS T_WC_TMDB_IMAGE_CAPTION_REVIEW (
    ID_IMAGE_CAPTION_REVIEW BIGINT       NOT NULL AUTO_INCREMENT,
    EXPERIMENT        VARCHAR(64)  NOT NULL COMMENT 'ex. ab-context-20261025',
    TABLE_SOURCE      VARCHAR(16)  NOT NULL COMMENT 'movie | serie | season | episode | ...',
    ID_ROW            BIGINT       NOT NULL COMMENT 'ID_ROW de la ligne T_WC_TMDB_*_IMAGE source',
    ID_ENTITY         BIGINT       NOT NULL COMMENT 'ID_MOVIE, ID_SERIE, ... selon TABLE_SOURCE',
    IMAGE_PATH        VARCHAR(255) NOT NULL COMMENT 'chemin TMDb, ex. /abc.jpg',
    CAPTION_MODEL     VARCHAR(64)  NOT NULL COMMENT 'modèle des captions jugées',
    VARIANT_A         VARCHAR(128) NOT NULL COMMENT 'variante A (ou unique), ex. full: 25 cast lines, whole plot',
    CAPTION_A         TEXT         NOT NULL,
    VARIANT_B         VARCHAR(128) NULL,
    CAPTION_B         TEXT         NULL,
    VERDICT           VARCHAR(16)  NOT NULL COMMENT 'A | B | EQUAL | BOTH_WRONG | OK | WRONG',
    REVIEW_COMMENT    TEXT         NULL     COMMENT 'remarque libre du relecteur',
    REVIEWER          VARCHAR(32)  NOT NULL COMMENT 'ex. vaugouin',
    TIM_CREATED       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    TIM_UPDATED       TIMESTAMP    NULL     DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (ID_IMAGE_CAPTION_REVIEW),
    UNIQUE KEY UK_IMAGE_CAPTION_REVIEW (EXPERIMENT, TABLE_SOURCE, ID_ROW, REVIEWER),
    KEY ID_ENTITY (ID_ENTITY),
    KEY VERDICT (VERDICT)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
