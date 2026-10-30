-- Exemplo de sanitize pós-clone na TEMP (Aurora PostgreSQL).
-- Ajuste schemas/tabelas/colunas ao seu ambiente.
-- Use via SANITIZE_SQL_PATH=./sql/sanitize.example.pg.sql

UPDATE "labAPPemPROD".usuarios
SET
   email = 'user+' || id::text || '@example.invalid',
   senha_hash = repeat('0', 64)
WHERE id > 0;
