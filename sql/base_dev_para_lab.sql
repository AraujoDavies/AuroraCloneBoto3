CREATE SCHEMA IF NOT EXISTS leads DEFAULT CHARACTER SET utf8mb4;

USE leads;

DROP TABLE IF EXISTS dados_pessoais;

CREATE TABLE dados_pessoais (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    nome VARCHAR(100) NOT NULL,
    email VARCHAR(150) NOT NULL UNIQUE,
    telefone VARCHAR(20) NULL,
    empresa VARCHAR(100) NULL,
    origem ENUM('site','indicacao','linkedin','evento','anuncio') NOT NULL,
    status ENUM('novo','contatado','qualificado','negociacao','ganho','perdido') NOT NULL DEFAULT 'novo',
    responsavel_id INT UNSIGNED NULL,
    data_criacao DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ultimo_contato DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO dados_pessoais (nome, email, telefone, empresa, origem, status, responsavel_id, data_criacao, ultimo_contato) VALUES
('Caio Ribeiro',     'caio.ribeiro@lead-fake.com',     '(11) 90000-0001', 'Alfa Tecnologia',     'site',      'novo',        5,    '2026-10-26 10:15:00', NULL),
('Débora Lopes',     'debora.lopes@lead-fake.com',     '(21) 90000-0002', 'Beta Logística',      'linkedin',  'contatado',   5,    '2026-10-20 14:30:00', '2026-10-22 09:10:00'),
('Eduardo Pinto',    'eduardo.pinto@lead-fake.com',    '(31) 90000-0003', 'Gama Alimentos',      'indicacao', 'qualificado', 2,    '2026-10-10 09:00:00', '2026-10-25 16:45:00'),
('Fernanda Araújo',  'fernanda.araujo@lead-fake.com',  '(41) 90000-0004', 'Delta Educação',      'evento',    'negociacao',  3,    '2026-09-28 11:20:00', '2026-10-27 10:00:00'),
('Gustavo Moreira',  'gustavo.moreira@lead-fake.com',  NULL,              'Ômega Consultoria',   'anuncio',   'perdido',     2,    '2026-08-14 16:05:00', '2026-09-02 13:30:00'),
('Helena Campos',    'helena.campos@lead-fake.com',    '(51) 90000-0006', 'Sigma Saúde',         'site',      'ganho',       1,    '2026-07-03 08:40:00', '2026-10-15 15:20:00'),
('Igor Batista',     'igor.batista@lead-fake.com',     '(61) 90000-0007', 'Lambda Engenharia',   'linkedin',  'novo',        NULL, '2026-10-28 07:50:00', NULL),
('Juliana Cunha',    'juliana.cunha@lead-fake.com',    '(71) 90000-0008', 'Kappa Varejo',        'indicacao', 'contatado',   3,    '2026-10-18 13:10:00', '2026-10-21 11:35:00'),
('Leandro Siqueira', 'leandro.siqueira@lead-fake.com', '(81) 90000-0009', NULL,                  'evento',    'qualificado', 1,    '2026-10-05 15:55:00', '2026-10-24 17:00:00'),
('Mônica Vieira',    'monica.vieira@lead-fake.com',    '(85) 90000-0010', 'Tau Construtora',     'anuncio',   'negociacao',  10,   '2026-09-15 10:25:00', '2026-10-12 09:45:00');

CREATE SCHEMA IF NOT EXISTS interno DEFAULT CHARACTER SET utf8mb4;

USE interno;

DROP TABLE IF EXISTS colaboradores;

CREATE TABLE colaboradores (
    id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    nome VARCHAR(100) NOT NULL,
    email VARCHAR(150) NOT NULL UNIQUE,
    cargo VARCHAR(80) NOT NULL,
    departamento VARCHAR(60) NOT NULL,
    data_admissao DATE NOT NULL,
    ativo TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO colaboradores (nome, email, cargo, departamento, data_admissao, ativo) VALUES
('Marcos Oliveira',  'marcos.oliveira@empresa-fake.com',  'Gerente Comercial',      'Vendas',     '2021-03-15', 1),
('Patrícia Gomes',   'patricia.gomes@empresa-fake.com',   'Executiva de Vendas',    'Vendas',     '2022-07-01', 1),
('Rafael Martins',   'rafael.martins@empresa-fake.com',   'Executivo de Vendas',    'Vendas',     '2023-01-09', 1),
('Sabrina Teixeira', 'sabrina.teixeira@empresa-fake.com', 'Analista de Marketing',  'Marketing',  '2022-11-21', 1),
('Thiago Barros',    'thiago.barros@empresa-fake.com',    'SDR',                    'Vendas',     '2024-02-12', 1),
('Vanessa Duarte',   'vanessa.duarte@empresa-fake.com',   'Analista de Suporte',    'Suporte',    '2023-05-30', 1),
('Wagner Freitas',   'wagner.freitas@empresa-fake.com',   'Administrador de TI',    'TI',         '2020-09-14', 1),
('Yasmin Cardoso',   'yasmin.cardoso@empresa-fake.com',   'Coordenadora de RH',     'RH',         '2021-08-02', 1),
('Zé Carlos Neto',   'ze.neto@empresa-fake.com',          'Estagiário',             'Marketing',  '2025-06-16', 1),
('Alice Fernandes',  'alice.fernandes@empresa-fake.com',  'Executiva de Vendas',    'Vendas',     '2022-04-18', 0);
