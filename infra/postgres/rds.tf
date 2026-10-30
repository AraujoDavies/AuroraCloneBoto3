resource "aws_rds_cluster" "lab" {
  for_each = local.labs

  cluster_identifier = each.value.cluster_identifier
  engine             = local.engine
  engine_mode        = "provisioned"
  engine_version     = var.engine_version != "" ? var.engine_version : null

  master_username = var.master_username
  master_password = each.value.master_password

  db_subnet_group_name   = aws_db_subnet_group.lab[each.key].name
  vpc_security_group_ids = [aws_security_group.aurora[each.key].id]

  serverlessv2_scaling_configuration {
    min_capacity             = var.min_capacity
    max_capacity             = var.max_capacity
    seconds_until_auto_pause = var.min_capacity == 0 ? var.seconds_until_auto_pause : null
  }

  backup_retention_period = 1
  deletion_protection     = false
  skip_final_snapshot     = true
  apply_immediately       = true
  storage_encrypted       = true

  tags = merge(local.common_tags, {
    Name = each.value.cluster_identifier
    Role = each.value.role
  })

  lifecycle {
    precondition {
      condition     = each.value.master_password != null && each.value.master_password != ""
      error_message = "A non-empty master password is required for ${each.key} (set prod_master_password or dev_master_password for the active deploy_mode)."
    }
  }
}

resource "terraform_data" "distinct_lab_passwords" {
  count = var.deploy_mode == "prod_dev" ? 1 : 0

  lifecycle {
    precondition {
      condition = (
        var.prod_master_password != null
        && var.dev_master_password != null
        && var.prod_master_password != var.dev_master_password
      )
      error_message = "prod_master_password and dev_master_password must differ when deploy_mode is \"prod_dev\"."
    }
  }
}

resource "aws_rds_cluster_instance" "writer" {
  for_each = local.labs

  identifier         = each.value.cluster_identifier
  cluster_identifier = aws_rds_cluster.lab[each.key].id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.lab[each.key].engine
  engine_version     = aws_rds_cluster.lab[each.key].engine_version

  publicly_accessible = true

  performance_insights_enabled = false
  monitoring_interval          = 0

  tags = merge(local.common_tags, {
    Name = each.value.cluster_identifier
    Role = each.value.role
  })
}
