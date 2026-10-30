-- Seed PROD (Aurora PostgreSQL) — schema presente na PROD (e no TEMP pós-clone).
-- Aplique no cluster PROD após o lab Terraform (psql -f ...).

CREATE SCHEMA IF NOT EXISTS "labAPPemPROD";

DROP TABLE IF EXISTS "labAPPemPROD".usuarios;

CREATE TABLE "labAPPemPROD".usuarios (
    id SERIAL PRIMARY KEY,
    nome VARCHAR(100) NOT NULL,
    email VARCHAR(150) NOT NULL UNIQUE,
    senha_hash CHAR(64) NOT NULL,
    ativo BOOLEAN NOT NULL DEFAULT TRUE,
    data_cadastro TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ultimo_login TIMESTAMP NULL
);

INSERT INTO "labAPPemPROD".usuarios (
    nome, email, senha_hash, ativo, data_cadastro, ultimo_login
) VALUES
('Ana Souza',      'ana.souza@exemplo.com',      encode(sha256('senha123'::bytea), 'hex'),  TRUE,  '2025-01-10 09:15:00', '2026-10-25 08:42:10'),
('Bruno Lima',     'bruno.lima@exemplo.com',     encode(sha256('abc@2025'::bytea), 'hex'),  TRUE,  '2025-02-03 14:20:00', '2026-10-27 19:05:33'),
('Carla Mendes',   'carla.mendes@exemplo.com',   encode(sha256('carla#456'::bytea), 'hex'), TRUE,  '2025-02-18 11:00:00', '2026-10-20 13:27:45'),
('Diego Alves',    'diego.alves@exemplo.com',    encode(sha256('diego!789'::bytea), 'hex'), FALSE, '2025-03-05 16:45:00', '2026-06-14 10:12:00'),
('Elisa Ramos',    'elisa.ramos@exemplo.com',    encode(sha256('elisa$321'::bytea), 'hex'), TRUE,  '2025-04-12 08:30:00', '2026-10-28 07:55:21'),
('Felipe Costa',   'felipe.costa@exemplo.com',   encode(sha256('felipe*654'::bytea), 'hex'),TRUE,  '2025-05-20 17:10:00', '2026-10-22 21:40:09'),
('Gabriela Nunes', 'gabriela.nunes@exemplo.com', encode(sha256('gabi&987'::bytea), 'hex'),  TRUE,  '2025-06-01 10:05:00', NULL),
('Henrique Dias',  'henrique.dias@exemplo.com',  encode(sha256('henri%147'::bytea), 'hex'), FALSE, '2025-07-15 12:50:00', '2026-03-02 15:33:18'),
('Isabela Rocha',  'isabela.rocha@exemplo.com',  encode(sha256('isa^258'::bytea), 'hex'),   TRUE,  '2025-08-09 09:00:00', '2026-10-26 11:18:50'),
('João Pereira',   'joao.pereira@exemplo.com',   encode(sha256('joao+369'::bytea), 'hex'),  TRUE,  '2025-09-30 18:25:00', '2026-10-24 06:47:02');

SELECT * FROM "labAPPemPROD".usuarios;
