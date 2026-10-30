-- Exemplo de sanitize pós-clone na TEMP (dados vindos de PROD).
-- Ajuste schemas/tabelas/colunas ao seu ambiente.
-- MySQL / Aurora MySQL: rode como está (ou via SANITIZE_SQL_PATH).
-- Aurora PostgreSQL: adapte sintaxe (aspas, schemas) e use o mesmo path.

UPDATE labAPPemPROD.usuarios
SET
   email = CONCAT('user+', id, '@example.invalid'),
   senha_hash = REPEAT('0', 64)
WHERE id > 0;