CREATE DATABASE IF NOT EXISTS labAPPemPROD;
USE labAPPemPROD;

DROP TABLE IF EXISTS usuarios;

CREATE TABLE usuarios (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    nome VARCHAR(100) NOT NULL,
    email VARCHAR(150) NOT NULL UNIQUE,
    senha_hash CHAR(64) NOT NULL,
    ativo TINYINT(1) NOT NULL DEFAULT 1,
    data_cadastro DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ultimo_login DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO usuarios (nome, email, senha_hash, ativo, data_cadastro, ultimo_login) VALUES
('Ana Souza',      'ana.souza@exemplo.com',      SHA2('senha123', 256),  1, '2025-01-10 09:15:00', '2026-10-25 08:42:10'),
('Bruno Lima',     'bruno.lima@exemplo.com',     SHA2('abc@2025', 256),  1, '2025-02-03 14:20:00', '2026-10-27 19:05:33'),
('Carla Mendes',   'carla.mendes@exemplo.com',   SHA2('carla#456', 256), 1, '2025-02-18 11:00:00', '2026-10-20 13:27:45'),
('Diego Alves',    'diego.alves@exemplo.com',    SHA2('diego!789', 256), 0, '2025-03-05 16:45:00', '2026-06-14 10:12:00'),
('Elisa Ramos',    'elisa.ramos@exemplo.com',    SHA2('elisa$321', 256), 1, '2025-04-12 08:30:00', '2026-10-28 07:55:21'),
('Felipe Costa',   'felipe.costa@exemplo.com',   SHA2('felipe*654', 256),1, '2025-05-20 17:10:00', '2026-10-22 21:40:09'),
('Gabriela Nunes', 'gabriela.nunes@exemplo.com', SHA2('gabi&987', 256),  1, '2025-06-01 10:05:00', NULL),
('Henrique Dias',  'henrique.dias@exemplo.com',  SHA2('henri%147', 256), 0, '2025-07-15 12:50:00', '2026-03-02 15:33:18'),
('Isabela Rocha',  'isabela.rocha@exemplo.com',  SHA2('isa^258', 256),   1, '2025-08-09 09:00:00', '2026-10-26 11:18:50'),
('João Pereira',   'joao.pereira@exemplo.com',   SHA2('joao+369', 256),  1, '2025-09-30 18:25:00', '2026-10-24 06:47:02');

SELECT * FROM usuarios;