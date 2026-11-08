# Role IAM do job de clone Aurora

Orientação para criar a **role/policy** usada pelo container/task que roda o clone.  
Objetivo: o job consegue clonar PROD → TEMP e gerenciar DEV/TEMP/OLD, mas **não consegue apagar (nem renomear) a base de produção**.

> O guard no código (`delete_rds`) é complementar. Sem Deny na IAM, bug, env errado ou rename mal configurado ainda pode apontar para o cluster errado.

---

## Princípios

1. Role **só** para este job — sem `AdministratorAccess` e sem `AmazonRDSFullAccess` na conta.
2. **Allow** mínimo nas APIs que o pipeline usa (Describe, Restore/PITR, Create, Modify, Delete).
3. **Deny explícito** de delete (e, se possível, modify destrutivo / rename) nos ARNs de **PROD**.
4. Preferir restringir Allow por **ARN** ou **tag** (TEMP / DEV / OLD), não por `*`.
5. Na AWS, manter **Deletion protection = true** no cluster de PROD (camada extra).

---

## O que o job precisa (Allow)

Ajuste conta, região e identificadores.

| Uso no fluxo | Ações RDS típicas | Recursos |
|---|---|---|
| Listar / aguardar status | `rds:DescribeDBClusters`, `rds:DescribeDBInstances` | `*` (ou conta/região) |
| Clone PROD → TEMP | `rds:RestoreDBClusterToPointInTime` | source = PROD; target = TEMP |
| Criar instância TEMP | `rds:CreateDBInstance` | cluster/instância TEMP |
| Rename DEV↔OLD, TEMP→DEV | `rds:ModifyDBCluster`, `rds:ModifyDBInstance` | só DEV, TEMP, OLD |
| Delete OLD (ou TEMP em falha) | `rds:DeleteDBInstance`, `rds:DeleteDBCluster` | só DEV antigo / TEMP / OLD |
| Credenciais (se usar SSM) | `ssm:GetParameter` (+ decrypt se SecureString) | parâmetros do DEV |
| Archive dumps/logs (se `S3_BUCKET`) | `s3:PutObject`, `s3:AbortMultipartUpload`, `s3:ListMultipartUploadParts` (+ `s3:ListBucket` no bucket) | `arn:aws:s3:::BUCKET/prefix*` |

Também costuma ser necessário: `iam:PassRole` só se a task/ECS precisar passar role a outro serviço; e tags (`rds:AddTagsToResource`) se o fluxo marcar recursos.

### Archive S3 (opcional)

Se o job grava dumps/logs no S3 (`S3_BUCKET` no `.env`):

1. Prefira um **bucket dedicado**, Block Public Access **on**, sem ACL pública.
2. Configure **Lifecycle** para expirar versões atuais em ~**30 dias** (a retenção local no disco é outra: `DUMP_LOCAL_RETENTION_DAYS`, default 15).
3. Restrinja Put ao prefixo (`S3_DUMP_PREFIX`) quando possível.
4. O Allow de S3 fica na policy mínima do job (`policy-clone-minimo.json`), não em `make generate-policy`.

---

## O que deve ser bloqueado na PROD (Deny)

Negue **sempre**, mesmo que algum Allow ampliado exista:

- `rds:DeleteDBCluster`
- `rds:DeleteDBInstance`
- Idealmente também rename da PROD:
  - `rds:ModifyDBCluster` (quando muda identificador / remoção de proteção)
  - `rds:ModifyDBInstance` (idem)

Escopo do Deny: ARN do **cluster** e da **instância** de produção (e variants se houver reader).

Exemplo de ARN:

```text
arn:aws:rds:<REGION>:<ACCOUNT_ID>:cluster:<PROD_CLUSTER_ID>
arn:aws:rds:<REGION>:<ACCOUNT_ID>:db:<PROD_INSTANCE_ID>
```

No projeto de referência atual:

- Cluster PROD: `mysqlmanageasyv2-aurora-cluster`
- Instância PROD: `mysqlmanageasyv2-aurora`

Substitua pelos IDs da sua conta antes de aplicar.

---

## Modelo de policy (esqueleto)

Gere a policy com os ARNs reais da conta via:

```bash
make generate-policy
```

Isso usa STS + `PROD_IDENTIFIER` e escreve `policy.json` só com o Deny de delete/rename na PROD (policy `AvoidDeleteAuroraCloneBoto3`). Os Allows do job ficam em `policy-clone-minimo.json`. O JSON abaixo é o esqueleto completo de referência, se for montar Allows e Deny na mesma policy à mão.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DescribeRds",
      "Effect": "Allow",
      "Action": [
        "rds:DescribeDBClusters",
        "rds:DescribeDBInstances"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloneProdToTemp",
      "Effect": "Allow",
      "Action": [
        "rds:RestoreDBClusterToPointInTime"
      ],
      "Resource": [
        "arn:aws:rds:REGION:ACCOUNT_ID:cluster:PROD_CLUSTER_ID",
        "arn:aws:rds:REGION:ACCOUNT_ID:cluster:TEMP_CLUSTER_ID"
      ]
    },
    {
      "Sid": "ManageTempDevOld",
      "Effect": "Allow",
      "Action": [
        "rds:CreateDBInstance",
        "rds:ModifyDBCluster",
        "rds:ModifyDBInstance",
        "rds:DeleteDBInstance",
        "rds:DeleteDBCluster",
        "rds:AddTagsToResource"
      ],
      "Resource": [
        "arn:aws:rds:REGION:ACCOUNT_ID:cluster:TEMP_CLUSTER_ID",
        "arn:aws:rds:REGION:ACCOUNT_ID:db:TEMP_INSTANCE_ID",
        "arn:aws:rds:REGION:ACCOUNT_ID:cluster:DEV_CLUSTER_ID",
        "arn:aws:rds:REGION:ACCOUNT_ID:db:DEV_INSTANCE_ID",
        "arn:aws:rds:REGION:ACCOUNT_ID:cluster:OLD_CLUSTER_ID",
        "arn:aws:rds:REGION:ACCOUNT_ID:db:OLD_INSTANCE_ID"
      ]
    },
    {
      "Sid": "DenyDeleteOrRenameProd",
      "Effect": "Deny",
      "Action": [
        "rds:DeleteDBCluster",
        "rds:DeleteDBInstance",
        "rds:ModifyDBCluster",
        "rds:ModifyDBInstance"
      ],
      "Resource": [
        "arn:aws:rds:REGION:ACCOUNT_ID:cluster:PROD_CLUSTER_ID",
        "arn:aws:rds:REGION:ACCOUNT_ID:db:PROD_INSTANCE_ID"
      ]
    },
    {
      "Sid": "ReadDbPasswordFromSsm",
      "Effect": "Allow",
      "Action": [
        "ssm:GetParameter",
        "ssm:GetParameters"
      ],
      "Resource": [
        "arn:aws:ssm:REGION:ACCOUNT_ID:parameter/aurora-clone/dev/db_password"
      ]
    }
  ]
}
```

Se o parâmetro for **SecureString**, a role também precisa de `kms:Decrypt` na chave usada pelo parâmetro (chave default da conta ou CMK).

Notas:

- O Deny de `Modify*` na PROD impede rename acidental da produção. O clone (`RestoreDBClusterToPointInTime`) **não** precisa de Modify na PROD.
- Se o Allow de Create/Delete usar tags (`aws:ResourceTag/...`), mantenha o Deny por ARN da PROD mesmo assim.
- O statement SSM cobre `DB_PASSWORD_SSM_PARAM` quando `DB_PASSWORD` não está no env; ajuste o ARN ao path real.
- Se `S3_BUCKET` estiver setada, o Allow de PutObject (e multipart) fica em `policy-clone-minimo.json`. `make generate-policy` não inclui S3.

---

## Como anexar a role

1. Crie a role de execução do job (ex.: task role ECS / role do host / IRSA).
2. Anexe a policy acima (managed customer ou inline).
3. Garanta que o processo **não** use access key de usuário admin — só essa role.
4. Confirme Deletion protection no cluster PROD no console RDS.
